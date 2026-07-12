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

import asyncio
import logging
import re
from typing import Awaitable, Callable

from recipe_scrapers import scrape_html
import aiohttp

from config import LLM_TEXT_MODEL, LLM_TRANSLATE_MODEL, LLM_VISION_MODEL
from processing import llm
from processing.safe_url import assert_url_safe
from status_helpers import source_name

log = logging.getLogger(__name__)


class IngestError(Exception):
    """Raised by `ingest_recipe` when the parse step yields nothing useful.

    Callers catch this once and surface a uniform user-facing error
    instead of re-implementing the "None means failed" check at every
    site. Distinct from `processing.llm.LLMError` because the parse can
    fail without ever touching the LLM (e.g. unreachable host, captcha
    page, image decode bust).
    """


# -- ingest_recipe behavior matrix ------------------------------------------
# Cheat sheet for the policy each surface had before this function existed.
# `ingest_recipe(source, *, push, persist)` is the single entry point now;
# every caller just picks the right flags.
#
#   surface              source  push   persist  notes
#   ─────────────────────────────────────────────────────────────────────────
#   web URL-add          URL     False  True     dedupes via find_by_url;
#                                                lands on detail page.
#   web photo-upload     bytes   False  True     same, but OCR'd; `hint`
#                                                is the filename.
#   bot URL-paste        URL     True   False    pushes immediately, stashes
#                                                a pending token so the
#                                                💾 Save button can persist
#                                                later. The bot still owns
#                                                _stash_pending — ingest
#                                                returns the parsed dict.
#   bot photo-upload     bytes   True   False    same; `hint` is the photo
#                                                caption.
#   scheduler Fooby      URL     True   False    transient "inspiration of
#                                                the day" — user can still
#                                                save it later by re-pasting.
#   scheduler anniversary — — —  reused push_recipe_to_display directly
#                                with a library row (no ingest needed).
#
# The "skip if already on display" check the scheduler used to do by hand
# now lives in display_push.push_recipe_to_display for the persisted
# branch, and in ingest_recipe itself for the parse-only-push branch
# (compares the active display URL to the just-parsed URL). Callers see
# action == "already-active" and can log accordingly.
# ---------------------------------------------------------------------------

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


async def close_session() -> None:
    """Close the shared fetch session. Called from main()'s shutdown path."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


async def process_recipe_url(
    url: str,
    *,
    on_llm_start=None,
) -> dict | None:
    """Fetch a URL and extract structured recipe data.

    Returns a dict with title, total_time, servings, ingredients, instructions, lang.
    Instructions is a list of dicts: {"type": "step"|"heading", "text": "..."}

    Falls back through embedded JSON-LD and an LLM call when
    recipe-scrapers can't make sense of the page. Returns None only when
    all three paths have failed.

    `on_llm_start` is an optional async callable fired once, just before
    the LLM step actually runs (so only when both faster tiers missed
    AND the LLM is configured). The Telegram bot uses it to swap the
    placeholder message into a "Converting with an LLM…" state.
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

    recipe = await _try_llm(url, html, on_start=on_llm_start)
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
        log.debug("Raw instructions:\n%s", raw_instructions)

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

        log.info(
            "Parsed recipe: %s (lang=%s, %d ingredients, %d instructions, "
            "time=%s, servings=%s)",
            recipe["title"], lang, len(recipe["ingredients"]),
            len(recipe["instructions"]), recipe["total_time"], recipe["servings"],
        )
        # Full payloads only at DEBUG — one line per user action at INFO.
        log.debug("  Ingredients: %s", recipe["ingredients"])
        log.debug("  Instructions: %s", recipe["instructions"])

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


async def _try_llm(url: str, html: str, on_start=None) -> dict | None:
    """Cleaned-HTML → LLM → validated recipe dict.

    Returns None when the LLM isn't configured, when the call fails, or
    when the model's output doesn't pass the validator. Each of those is
    a clean failure mode; the caller logs and surfaces a user-facing
    error.

    `on_start` (optional async callable) fires once, just before the
    actual chat-completions request — but only after we've confirmed the
    LLM is enabled and we have a non-empty blob. The Telegram bot uses
    it to swap the placeholder message into an "LLM is working" state.
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

    if on_start is not None:
        try:
            await on_start()
        except Exception:
            # The hook is decorative; a failure (e.g. Telegram edit race)
            # must not abort the actual recipe parse.
            log.debug("on_llm_start hook failed", exc_info=True)

    log.info("Calling LLM (%s) with %d chars of cleaned text", LLM_TEXT_MODEL, len(blob))
    try:
        raw = await llm.complete_json(
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


async def process_recipe_image(
    image_bytes: bytes,
    hint: str | None = None,
) -> tuple[dict, str] | None:
    """OCR a recipe photo into ePepper's internal recipe dict.

    Returns `(recipe, url)` where `url` is a `cookbook://<source>/<slug>`
    surrogate built from the LLM's `source_name` (when readable, e.g.
    the cookbook title visible on a cover/spine) and the recipe title.
    Source-less photos collapse to `cookbook://cookbook/<slug>` and
    further to a content hash via the existing `resolve_url` helper.

    `hint` is optional sender-supplied context — the Telegram caption or
    the (cleaned) web upload filename. The OCR prompt treats it as
    ground truth for `source_name` / title disambiguation, which is the
    fix for the common "Photo shows the recipe page but not the cover"
    failure mode.

    Returns None when the LLM is unconfigured, the call fails, or the
    output doesn't satisfy the minimum recipe contract — same contract
    the URL path uses, so the caller can surface a uniform error.
    """
    if not llm.is_enabled():
        log.info("OCR skipped — LLM_API_URL / LLM_API_KEY unset")
        return None

    from processing.images import encode_for_ocr
    from processing.jsonld import resolve_url
    from processing.prompts import OCR_SYSTEM, ocr_user

    try:
        jpeg = encode_for_ocr(image_bytes)
    except Exception as e:
        log.warning("OCR: image decode failed: %s", e)
        return None
    log.info(
        "OCR: calling %s with %d byte JPEG (hint=%r)",
        LLM_VISION_MODEL, len(jpeg), hint or "",
    )

    try:
        raw = await llm.complete_json(
            model=LLM_VISION_MODEL,
            system=OCR_SYSTEM,
            user=ocr_user(hint),
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


async def ingest_recipe(
    source: str | bytes | bytearray,
    *,
    push: bool,
    persist: bool,
    hint: str | None = None,
    on_llm_start: Callable[[], Awaitable[None]] | None = None,
) -> dict:
    """Canonical "parse → translate → upsert → save → push" pipeline.

    `source` dispatches by type: a `str` is a recipe URL (fed to
    `process_recipe_url`); `bytes` / `bytearray` is an image blob (fed
    to `process_recipe_image`, with `hint` passed through to the OCR
    prompt as user-supplied context — filename for web uploads, photo
    caption for Telegram).

    `push` controls whether the result lands on the e-ink panel.
    `persist` controls whether the recipe is written to the library
    (translate + upsert + save). The four (push, persist) combinations
    are all real surfaces — see the behavior matrix at the top of this
    module.

    `on_llm_start` is forwarded to `process_recipe_url` for URL sources
    (the Telegram bot uses it to swap its placeholder reply into a
    "Converting with an LLM…" state). Ignored for byte sources because
    OCR is always an LLM call — the caller can light the indicator
    itself before calling in.

    Returns `{recipe_id, url, recipe, action}` where `action` is one of:
      - "saved+pushed"  — persisted to the library AND drawn on the panel
      - "saved"         — persisted, no push
      - "pushed"        — drawn on the panel, not persisted
      - "parsed-only"   — parsed and returned; caller does the rest
      - "already-active" — push was requested but the panel was already
                          showing this recipe (skipped to save an e-ink
                          refresh); persist still ran if requested.

    Raises `IngestError` when the parse step yields no recipe — every
    parser (recipe-scrapers, JSON-LD, LLM) tried and missed. The caller
    catches once and renders a uniform "couldn't read a recipe" error.
    """
    # Lazy imports keep this module's import footprint untouched —
    # processing.recipes is pulled in by tests and tools that don't want
    # the library / display side-effects.
    from display import state as display_state
    import library
    from display.push import push_recipe_to_display

    # Dedupe lookup runs even when persist=False — the bot's URL-paste
    # path wants to know "is this already in the library so I should push
    # the saved row (with its Save-state) instead of stashing another
    # pending token?". When persist=True, the lookup also avoids a wasted
    # re-translate / re-upsert on an existing URL.
    existing: dict | None = None

    if isinstance(source, str):
        url = source
        # Dedupe BEFORE the parse: an already-known URL is pushed from its
        # stored row anyway, so re-fetching (and possibly paying an LLM
        # call) just to discard the fresh parse was pure waste. This also
        # matches the web add flow, which checks find_by_url first.
        existing = library.find_by_url(url)
        if existing is not None:
            recipe = existing["recipe"]
        else:
            recipe = await process_recipe_url(source, on_llm_start=on_llm_start)
            if recipe is None:
                raise IngestError(f"No recipe parsed from URL: {source}")
    elif isinstance(source, (bytes, bytearray)):
        image_bytes = bytes(source)
        result = await process_recipe_image(image_bytes, hint=hint)
        if result is None:
            raise IngestError("No recipe parsed from image bytes")
        recipe, url = result
        # Image sources derive their surrogate URL from the OCR output, so
        # the dedupe lookup can only happen after the parse.
        existing = library.find_by_url(url)
    else:
        raise TypeError(
            f"ingest_recipe source must be str or bytes, got {type(source).__name__}"
        )

    recipe_id: int | None = None
    persisted = False

    if persist:
        if existing is not None:
            recipe_id = existing["id"]
            log.info(
                "ingest_recipe: existing row id=%d url=%s — skipping translate/upsert",
                recipe_id, url,
            )
        else:
            recipe_id, tags = await persist_recipe(url, recipe)
            log.info(
                "ingest_recipe: persisted id=%d title=%r url=%s tags=%r",
                recipe_id, recipe.get("title"), url, tags,
            )
        persisted = True
    elif existing is not None:
        # Already in the library but the caller didn't ask for a persist
        # step — still surface the existing id so they can route the push
        # through push_recipe_to_display (bumps touch_displayed) and skip
        # the pending-stash UX.
        recipe_id = existing["id"]

    pushed = False
    already_active = False
    if push:
        if recipe_id is not None:
            # Re-read the row so push_recipe_to_display gets the canonical
            # shape (parsed_json + url + id + timestamps) — find_by_url +
            # upsert_recipe both return that shape, but re-reading is the
            # least-surprising contract.
            row = library.get_recipe(recipe_id)
            if row is None:
                # Shouldn't happen — we just wrote it. Treat as a soft
                # failure: the parse succeeded, persistence reported success,
                # but the row vanished. Surface as parsed-only so the caller
                # doesn't claim a push that didn't land.
                log.warning(
                    "ingest_recipe: row id=%d vanished between save and push",
                    recipe_id,
                )
            else:
                # push_recipe_to_display owns the skip-if-active check now;
                # a True return covers both "rendered+touched" and
                # "already on display, skipped". The bool collapses both
                # into success — we re-check display_state for the
                # already_active flag so the action string is honest.
                state_before = display_state.get()
                was_active = (
                    state_before.get("type") == "recipe"
                    and state_before.get("recipe_id") == row["id"]
                )
                ok = push_recipe_to_display(row)
                if not ok:
                    log.warning(
                        "ingest_recipe: push failed for id=%d", recipe_id,
                    )
                else:
                    pushed = True
                    if was_active:
                        already_active = True
        else:
            # Non-persisted push (Fooby inspiration, bot pending-stash). No
            # library row exists, so we render via display_state directly —
            # touch_displayed has nothing to update and skip-if-active is
            # checked against the URL since recipe_id is None on both sides.
            state_before = display_state.get()
            if (
                state_before.get("type") == "recipe"
                and state_before.get("url") == url
                and state_before.get("recipe_id") is None
            ):
                log.info(
                    "ingest_recipe: transient recipe url=%s already on display; skipping",
                    url,
                )
                pushed = True
                already_active = True
            else:
                try:
                    display_state.set_recipe(
                        recipe,
                        recipe_id=None,
                        url=url,
                    )
                except Exception:
                    log.exception(
                        "ingest_recipe: render failed for transient url=%s", url,
                    )
                else:
                    pushed = True
                    log.info(
                        "ingest_recipe: pushed transient recipe %r (%s)",
                        recipe.get("title"), url,
                    )

    if already_active:
        action = "already-active"
    elif persisted and pushed:
        action = "saved+pushed"
    elif persisted:
        action = "saved"
    elif pushed:
        action = "pushed"
    else:
        action = "parsed-only"

    return {
        "recipe_id": recipe_id,
        "url": url,
        "recipe": recipe,
        "action": action,
    }


async def persist_recipe(url: str, recipe: dict) -> tuple[int, list[str]]:
    """Translate → upsert → save → tag a parsed recipe. Returns (id, tags).

    The one canonical "write this recipe into the repertoire" sequence,
    shared by `ingest_recipe`'s persist branch and the bot's deferred
    Save button so the two can't drift (e.g. one auto-tagging and the
    other not). Translation and tag-pick failures are tolerated inside
    their helpers — a save never fails because the LLM hiccuped.
    """
    import library

    vocabulary = [t for t, _ in library.list_tags()]
    translated, tags = await asyncio.gather(
        translate_for_search(recipe), pick_tags(recipe, vocabulary),
    )
    recipe_id = library.upsert_recipe(
        url, recipe,
        translated_keywords=translated,
        source=source_name(url),
    )
    library.save_recipe(recipe_id)
    if tags:
        library.set_tags(recipe_id, tags)
    return recipe_id, tags


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

    return normalize_recipe_for_render(_swissify({
        "title": title,
        "total_time": total_time,
        "servings": servings,
        "ingredients": ingredients,
        "instructions": instructions,
        "lang": lang,
    }))


def normalize_recipe_for_render(recipe: dict) -> dict:
    """Canonicalize the recipe shape so renderers don't have to dedupe at draw time.

    Applies the small set of structural cleanups that LLM extractions
    regularly trip over. Idempotent — running this twice on the same input
    is a no-op. The renderer-side rules ("a section of one step drops its
    N. prefix" on the BMP, ".step-solo" on the web) are *display* choices
    that consume this normalized list; they're not encoded here.

    Rules applied to `recipe["instructions"]`:
      - Drop items whose text is empty / whitespace-only.
      - Drop a heading whose text matches the most recently kept heading
        (the "Preparation → step → Preparation → step → …" pattern).
      - Of a run of consecutive headings with no step between, keep only
        the last (it's the one that actually introduces the next step).

    Returns a shallow-copied dict so callers can't accidentally mutate the
    input. The instructions list itself is freshly built; other fields are
    passed through by reference.
    """
    if not isinstance(recipe, dict):
        return recipe
    out = dict(recipe)
    items = recipe.get("instructions") or []
    cleaned: list[dict] = []
    last_heading_text: str | None = None
    for item in items:
        if not isinstance(item, dict):
            # Stray non-dict entries (validator usually catches these);
            # coerce as a step so we don't crash the renderer downstream.
            text = str(item).strip()
            if text:
                cleaned.append({"type": "step", "text": text})
            continue
        kind = item.get("type", "step")
        text = (item.get("text") or "").strip()
        if not text:
            continue  # drop empty heading/step
        if kind == "heading":
            if text == last_heading_text:
                continue  # drop duplicate-of-most-recent heading
            if cleaned and cleaned[-1].get("type") == "heading":
                # Consecutive heading run: overwrite the previous one
                # rather than appending a second underlined block.
                cleaned[-1] = {"type": "heading", "text": text}
            else:
                cleaned.append({"type": "heading", "text": text})
            last_heading_text = text
        else:
            cleaned.append({"type": "step", "text": text})
    out["instructions"] = cleaned
    return out


def _swissify(recipe: dict) -> dict:
    """Normalise German ß → ss across every text field.

    Asking the LLM to honour Swiss orthography via a prompt rule
    proved unreliable (it'd write "Strasse" half the time and
    "Straße" the other half). A post-pass over the validated recipe
    is cheap and deterministic. Unconditional rather than gated on
    lang=="de" because ß is German-only — no other language has
    characters it could legitimately produce.
    """
    recipe["title"] = recipe["title"].replace("ß", "ss")
    if recipe.get("servings"):
        recipe["servings"] = recipe["servings"].replace("ß", "ss")
    recipe["ingredients"] = [s.replace("ß", "ss") for s in recipe["ingredients"]]
    for step in recipe["instructions"]:
        step["text"] = step["text"].replace("ß", "ss")
    return recipe


async def translate_for_search(recipe: dict) -> str | None:
    """Return a flat FR/DE keyword blob for indexing into FTS, or None.

    Calls the (cheaper) LLM_TRANSLATE_MODEL with the recipe's title and
    ingredient list and asks for noun-form keywords in French + German.
    The result is space-joined into a single string ready to drop into
    `recipes.translated_keywords` and `recipes_fts.translated`.

    Returns None when the LLM is unconfigured, when the call fails, or
    when the output doesn't validate. Translation is decorative — a
    None return means the recipe still saves, just without bilingual
    search coverage (the backfill loop retries on startup).
    """
    if not llm.is_enabled():
        return None

    title = (recipe.get("title") or "").strip()
    ingredients = recipe.get("ingredients") or []
    if not title and not ingredients:
        return None
    native_lang = recipe.get("lang") or "en"

    from processing.prompts import TRANSLATE_SYSTEM, translate_user

    try:
        raw = await llm.complete_json(
            model=LLM_TRANSLATE_MODEL,
            system=TRANSLATE_SYSTEM,
            user=translate_user(title, ingredients, native_lang),
            # Keywords are short — a tight cap saves runaway-output cost
            # if the model decides to monologue.
            max_tokens=512,
        )
    except llm.LLMError as e:
        log.info("Translation failed for %r: %s", title, e)
        return None

    keywords: list[str] = []
    for code in ("fr", "de"):
        val = raw.get(code)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str) and item.strip():
                    keywords.append(item.strip())
    if not keywords:
        log.info("Translation produced no keywords for %r", title)
        return None
    blob = " ".join(keywords).replace("ß", "ss")
    log.info("Translation keywords for %r (%d terms)", title, len(keywords))
    return blob


# Cap on tags a single recipe gets auto-picked — a handful of genuinely
# relevant tags reads as organization; a dozen reads as noise.
_MAX_SUGGESTED_TAGS = 4

# Cap on how many existing tags get offered to the model as candidates.
# Bounds prompt size and keeps a small model from getting lost in a huge
# option list — list_tags() is frequency-sorted, so this keeps the tags
# a household actually uses, dropping only long-tail one-offs.
_MAX_TAG_VOCABULARY = 40


async def pick_tags(recipe: dict, vocabulary: list[str]) -> list[str]:
    """Return 0-N tags for `recipe`, chosen by the LLM from `vocabulary`.

    Never invents a tag: the model's raw output is filtered against
    `vocabulary` (case-insensitively) before anything is returned, since
    `llm.complete_json` only guarantees valid JSON — nothing about the
    values. Returns [] when the LLM is unconfigured, the vocabulary is
    empty (nothing to pick from — the natural state before a household
    has tagged anything by hand), the call fails, or validation yields
    nothing usable. Like `translate_for_search`, this is decorative: a
    [] return must never block a save.

    `vocabulary` is capped to `_MAX_TAG_VOCABULARY` here so every caller
    can just pass `library.list_tags()`'s tag names (already
    frequency-sorted) without re-implementing the cap.
    """
    if not llm.is_enabled() or not vocabulary:
        return []
    vocabulary = vocabulary[:_MAX_TAG_VOCABULARY]

    title = (recipe.get("title") or "").strip()
    ingredients = recipe.get("ingredients") or []
    if not title and not ingredients:
        return []

    from processing.prompts import TAGS_SYSTEM, tags_user

    try:
        raw = await llm.complete_json(
            model=LLM_TRANSLATE_MODEL,
            system=TAGS_SYSTEM,
            user=tags_user(title, ingredients, vocabulary),
            # A handful of short tag strings — no need for a big budget.
            max_tokens=256,
        )
    except llm.LLMError as e:
        log.info("Tag pick failed for %r: %s", title, e)
        return []

    vocab_lower = {t.lower(): t for t in vocabulary}
    tags: list[str] = []
    val = raw.get("tags")
    if isinstance(val, list):
        for item in val:
            if not isinstance(item, str):
                continue
            canon = vocab_lower.get(item.strip().lower())
            if canon and canon not in tags:
                tags.append(canon)
    tags = tags[:_MAX_SUGGESTED_TAGS]
    log.info("Tag pick for %r: %r", title, tags)
    return tags


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
    """Fetch HTML content from a URL, capped at `_MAX_HTML_BYTES`.

    `assert_url_safe` rejects a URL that resolves to a private / loopback /
    metadata address before we connect. The client follows redirects
    normally; only the initial URL is SSRF-checked (a redirect into the LAN
    is an accepted residual risk — the URLs come from two trusted users).
    """
    await assert_url_safe(url)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ePepper/1.0; recipe display)"
    }
    session = _get_session()
    timeout = aiohttp.ClientTimeout(total=15)
    async with session.get(url, headers=headers, timeout=timeout) as resp:
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
