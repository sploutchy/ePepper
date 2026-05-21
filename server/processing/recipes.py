"""Recipe URL processing via recipe-scrapers, with an LLM fallback.

Order of attempts in `process_recipe_url`:
  1. recipe-scrapers (site-specific parser, then generic wild_mode)
  2. embedded JSON-LD via html_extract (a free win — many sites that
     break recipe-scrapers still ship clean schema.org Recipe blobs)
  3. LLM extraction over a preprocessed text blob (Infomaniak AI Tools
     by default; configured via LLM_API_URL / LLM_API_KEY)

Each step is conditional on the previous one returning None, so a
well-behaved site never touches the LLM and never spends a token.
"""

import logging
import re

from recipe_scrapers import scrape_html
import aiohttp

from config import LLM_TEXT_MODEL, LLM_VISION_MODEL
from processing import llm
from processing.safe_url import assert_url_safe

log = logging.getLogger(__name__)

# Cap on HTML body size we'll pull from a recipe URL. Big enough for any
# real recipe page (a generous one is ~1 MB with images inlined as data:),
# small enough that a hostile / runaway endpoint can't OOM the server.
_MAX_HTML_BYTES = 5 * 1024 * 1024


class _ResponseTooLarge(Exception):
    """Raised when the HTTP body exceeds the configured cap."""


# Reused across calls so we're not paying TCP+TLS handshake + DNS for every
# recipe fetch. Lazily created on the running event loop; recreated if it's
# been closed (e.g. test teardown).
_session: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def process_recipe_url(url: str) -> dict | None:
    """Fetch a URL and extract structured recipe data.

    Returns a dict with title, total_time, servings, ingredients, instructions, lang.
    Instructions is a list of dicts: {"type": "step"|"heading", "text": "..."}

    Falls back through embedded JSON-LD and an LLM call when
    recipe-scrapers can't make sense of the page. Returns None only when
    all three paths have failed.
    """
    log.info("Fetching recipe from: %s", url)

    try:
        html = await _fetch_html(url)
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        return None
    log.info("Fetched %d bytes of HTML", len(html))

    recipe = _try_scraper(url, html)
    if recipe is not None:
        return recipe

    recipe = _try_embedded_jsonld(url, html)
    if recipe is not None:
        log.info("Recipe sourced from embedded JSON-LD (no LLM call)")
        return recipe

    recipe = await _try_llm(url, html)
    if recipe is not None:
        log.info("Recipe sourced from LLM fallback")
        return recipe

    log.warning("All recipe parsers failed for %s", url)
    return None


def _try_scraper(url: str, html: str) -> dict | None:
    """recipe-scrapers path — the cheap, fast default."""
    try:
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

        # No instructions = "scraper found nothing useful" — let the
        # fallbacks try. ePepper renders steps as the load-bearing surface;
        # an ingredients-only parse is a shopping list, not a recipe (seen
        # in the wild on bio-mio.ch, where recipe-scrapers extracts the
        # ingredient table but misses the steps).
        if not recipe["instructions"]:
            log.info(
                "recipe-scrapers returned no instructions for %s "
                "(ingredients=%d) — falling through to JSON-LD / LLM",
                url, len(recipe["ingredients"]),
            )
            return None

        log.info("Parsed recipe: %s (lang=%s)", recipe["title"], lang)
        log.info("  Ingredients (%d): %s", len(recipe["ingredients"]), recipe["ingredients"])
        log.info("  Instructions (%d): %s", len(recipe["instructions"]), recipe["instructions"])
        log.info("  Time: %s, Servings: %s", recipe["total_time"], recipe["servings"])

        return recipe

    except Exception as e:
        log.info("recipe-scrapers failed for %s: %s", url, e)
        return None


def _try_embedded_jsonld(url: str, html: str) -> dict | None:
    """Pull a schema.org Recipe out of `<script type=application/ld+json>`.

    Free of LLM cost — many sites that defeat recipe-scrapers (because
    of unusual CSS or HTML structure) still publish valid JSON-LD blobs.
    Imported lazily so the legacy import surface (process_recipe_url) stays
    cheap when LLM features are disabled.
    """
    try:
        from processing.html_extract import extract

        recipe_payload = extract(html)
    except Exception as e:
        log.info("html_extract failed for %s: %s", url, e)
        return None
    if recipe_payload is None:
        return None
    # Same rule as _try_scraper — without steps, ePepper has nothing to
    # render. Fall through to the LLM. Seen on bio-mio.ch, which ships an
    # ingredient-only JSON-LD Recipe blob.
    if not recipe_payload.get("instructions"):
        log.info(
            "JSON-LD Recipe had no instructions for %s "
            "(ingredients=%d) — falling through to LLM",
            url, len(recipe_payload.get("ingredients") or []),
        )
        return None
    return recipe_payload


async def _try_llm(url: str, html: str) -> dict | None:
    """Cleaned-HTML → LLM → validated recipe dict.

    Returns None when the LLM isn't configured, when the call fails, or
    when the model's output doesn't pass the validator. Each of those is
    a clean failure mode; the caller logs and surfaces a user-facing
    error.
    """
    if not llm.is_enabled():
        log.info("LLM fallback disabled (LLM_API_URL / LLM_API_KEY unset)")
        return None

    from processing.html_extract import to_text
    from processing.prompts import URL_SYSTEM, url_user

    blob = to_text(html)
    if not blob:
        log.info("Empty preprocessed blob, skipping LLM call")
        return None

    log.info("Calling LLM (%s) with %d chars of cleaned text", LLM_TEXT_MODEL, len(blob))
    try:
        raw = await llm.complete_json(
            kind="url",
            model=LLM_TEXT_MODEL,
            system=URL_SYSTEM,
            user=url_user(url, blob),
        )
    except llm.LLMError as e:
        log.warning("LLM fallback failed for %s: %s", url, e)
        return None

    recipe = validate_llm_recipe(raw)
    if recipe is None:
        log.warning("LLM output didn't validate for %s", url)
        return None
    return recipe


async def process_recipe_image(image_bytes: bytes) -> tuple[dict, str] | None:
    """OCR a recipe photo into ePepper's internal recipe dict.

    Returns `(recipe, url)` where `url` is a `cookbook://<source>/<slug>`
    surrogate built from the LLM's `source_name` (when readable, e.g.
    the cookbook title visible on a cover/spine) and the recipe title.
    Source-less photos collapse to `cookbook://cookbook/<slug>` and
    further to a content hash via the existing `resolve_url` helper.

    Returns None when the LLM is unconfigured, the call fails, or the
    output doesn't satisfy the minimum recipe contract — same contract
    the URL path uses, so the caller can surface a uniform error.
    """
    if not llm.is_enabled():
        log.info("OCR skipped — LLM_API_URL / LLM_API_KEY unset")
        return None

    from processing.images import encode_for_ocr
    from processing.jsonld import resolve_url
    from processing.prompts import OCR_SYSTEM, OCR_USER

    try:
        jpeg = encode_for_ocr(image_bytes)
    except Exception as e:
        log.warning("OCR: image decode failed: %s", e)
        return None
    log.info("OCR: calling %s with %d byte JPEG", LLM_VISION_MODEL, len(jpeg))

    try:
        raw = await llm.complete_json(
            kind="ocr",
            model=LLM_VISION_MODEL,
            system=OCR_SYSTEM,
            user=OCR_USER,
            image_jpeg=jpeg,
        )
    except llm.LLMError as e:
        log.warning("OCR LLM call failed: %s", e)
        return None

    recipe = validate_llm_recipe(raw)
    if recipe is None:
        log.warning("OCR: LLM output didn't validate")
        return None

    source_name = ""
    src = raw.get("source_name")
    if isinstance(src, str):
        source_name = src.strip()

    url = _ocr_url(source_name, recipe["title"])
    url = resolve_url(url, recipe)  # collapses to content hash if empty
    log.info("OCR: recipe %r url=%s source=%r", recipe["title"], url, source_name)
    return recipe, url


_SLUG_KEEP_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    """ASCII kebab-case slug; empty if `text` carries no ASCII letters/digits."""
    import unicodedata

    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    norm = norm.lower()
    return _SLUG_KEEP_RE.sub("-", norm).strip("-")


def _ocr_url(source_name: str, title: str) -> str:
    """Build the `cookbook://<source>/<title>` surrogate URL for an OCR recipe.

    Falls back to `cookbook://cookbook/<title>` when no source was read,
    and to bare `cookbook://` when the title slugifies to nothing — at
    which point `resolve_url` swaps in a content hash so the library's
    UNIQUE(url) still dedupes.
    """
    src_slug = _slug(source_name) or "cookbook"
    title_slug = _slug(title)
    if not title_slug:
        return "cookbook://"
    return f"cookbook://{src_slug}/{title_slug}"


def validate_llm_recipe(raw: dict) -> dict | None:
    """Coerce an LLM-produced JSON object into ePepper's internal recipe shape.

    Tolerant — accepts string total_time, missing optional fields, list
    or string ingredients — but enforces the minimum contract: a title
    and at least one of ingredients/instructions, with a lang we
    actually render. Returns None when those minimums aren't met so the
    caller can surface a clear error instead of pushing a junk recipe
    into the library.
    """
    if not isinstance(raw, dict):
        return None

    title = _str_or_empty(raw.get("title")).strip()
    if not title:
        return None

    ingredients = _coerce_ingredients(raw.get("ingredients"))
    instructions = _coerce_instructions(raw.get("instructions"))
    if not ingredients and not instructions:
        return None

    total_time = _coerce_int(raw.get("total_time"))
    servings = _str_or_empty(raw.get("servings")).strip() or None

    lang_raw = _str_or_empty(raw.get("lang")).strip().lower()
    lang = lang_raw if lang_raw in ("en", "de", "fr", "it") else "en"

    return {
        "title": title,
        "total_time": total_time,
        "servings": servings,
        "ingredients": ingredients,
        "instructions": instructions,
        "lang": lang,
    }


def _str_or_empty(v) -> str:
    return v if isinstance(v, str) else ""


def _coerce_int(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v > 0 else None
    if isinstance(v, float):
        return int(v) if v > 0 else None
    if isinstance(v, str):
        m = re.search(r"\d+", v)
        if m:
            n = int(m.group(0))
            return n if n > 0 else None
    return None


def _coerce_ingredients(v) -> list[str]:
    if isinstance(v, str):
        return [line.strip() for line in v.splitlines() if line.strip()]
    if isinstance(v, list):
        out = []
        for item in v:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                # tolerate the LLM emitting {name, qty, unit} shapes
                txt = item.get("text") or item.get("name") or ""
                if isinstance(txt, str) and txt.strip():
                    out.append(txt.strip())
        return out
    return []


def _coerce_instructions(v) -> list[dict]:
    """Accept the canonical [{type, text}, …] shape, plus a few stragglers.

    Stragglers we tolerate: a plain string (newline-split), a list of
    plain strings (each becomes a step), a list with a string `step`
    instead of the typed dict.
    """
    if isinstance(v, str):
        return [
            {"type": "step", "text": line.strip()}
            for line in v.splitlines()
            if line.strip()
        ]
    if not isinstance(v, list):
        return []
    out: list[dict] = []
    for item in v:
        if isinstance(item, str) and item.strip():
            out.append({"type": "step", "text": item.strip()})
            continue
        if not isinstance(item, dict):
            continue
        t = item.get("type", "step")
        text = item.get("text") or item.get("step") or item.get("name") or ""
        if not isinstance(text, str) or not text.strip():
            continue
        kind = "heading" if t == "heading" else "step"
        out.append({"type": kind, "text": text.strip()})
    return out


async def _fetch_html(url: str) -> str:
    """Fetch HTML content from a URL, capped at `_MAX_HTML_BYTES`."""
    await assert_url_safe(url)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ePepper/1.0; recipe display)"
    }
    session = _get_session()
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        log.info("HTTP %d from %s", resp.status, url)
        resp.raise_for_status()
        # Fast path: trust an honest Content-Length header when present.
        declared = resp.content_length
        if declared is not None and declared > _MAX_HTML_BYTES:
            raise _ResponseTooLarge(
                f"{url} declared {declared} bytes (cap {_MAX_HTML_BYTES})"
            )
        # Stream so a server that lies about (or omits) Content-Length
        # still can't OOM us.
        buf = bytearray()
        async for chunk in resp.content.iter_chunked(64 * 1024):
            buf.extend(chunk)
            if len(buf) > _MAX_HTML_BYTES:
                raise _ResponseTooLarge(
                    f"{url} exceeded cap {_MAX_HTML_BYTES} mid-stream"
                )
        # A bogus Content-Type charset (unknown codec name) makes
        # bytes.decode raise LookupError *before* it consults errors=,
        # so fall back to utf-8 instead of crashing the fetch.
        charset = resp.charset or "utf-8"
        try:
            return buf.decode(charset, errors="replace")
        except LookupError:
            return buf.decode("utf-8", errors="replace")


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
    - Non-empty, short (1-4 words, under 40 chars)
    - No ending punctuation (no period, comma, etc.)
    - Starts with uppercase
    - Next line exists and is substantially longer (actual instruction text)
    """
    if not line or len(line) > 40:
        return False
    word_count = len(line.split())
    if word_count > 4:
        return False
    if line[-1] in ".,:;!?)":
        return False
    if not line[0].isupper():
        return False
    # Must have a following line that's meaningfully longer
    if not next_line:
        return False
    if len(next_line) > len(line) * 2:
        return True
    if word_count <= 2:
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
        if lang in ("de", "fr", "it", "en"):
            return lang

    # 2. Check URL for language hints
    url_lower = url.lower()
    for pattern, lang in [
        (r"/de/", "de"), (r"/fr/", "fr"), (r"/it/", "it"), (r"/en/", "en"),
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
