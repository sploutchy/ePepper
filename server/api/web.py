"""Web UI for browsing, searching, and managing the recipe library.

Server-rendered HTML + HTMX partials. Cookie-based auth: login checks the
shared API_KEY and sets a stateless cookie carrying an HMAC of the key (not
the key itself), httpOnly + Secure + SameSite=Lax. There's no server-side
session store — the cookie validates by recomputation, so rotating API_KEY
logs everyone out. Routes live under /app/ to keep the device-facing
endpoints clean.
"""

import hashlib
import hmac
import logging
import re
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import backup
import device_telemetry
from display import state as display_state
from processing import fooby_cache
import library
from config import API_KEY, PHOTO_MAX_MB, TZ

# Register the HEIF/HEIC opener with Pillow so iPhone .heic uploads decode
# without the user having to convert them first. Best-effort: a local dev
# env that hasn't re-pip'd yet should still import this module.
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass
from display.push import push_recipe_to_display
from processing.recipes import IngestError, ingest_recipe, normalize_recipe_for_render
from status_helpers import battery_pct, format_long_date, humanize_ago, humanize_date, rssi_quality, source_name

log = logging.getLogger(__name__)

# Hard cap on photo uploads. Phone shots after browser-side JPEG re-encode
# are typically 1-3 MB; the default 8 MB leaves headroom while still
# rejecting accidental drops. Tunable via PHOTO_MAX_MB in config.
_PHOTO_MAX_BYTES = PHOTO_MAX_MB * 1024 * 1024

COOKIE_NAME = "epepper_auth"
# 30-day cookie lifetime. The value is derived from the API key (see
# session_cookie_value), so it's stable across restarts; "logging out"
# beyond clearing the cookie means rotating API_KEY.
COOKIE_MAX_AGE = 30 * 24 * 3600

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
templates = Jinja2Templates(directory=str(_WEB_DIR / "templates"))


def session_cookie_value() -> str:
    """Stateless auth-cookie value derived from the API key.

    An HMAC of a fixed label keyed by API_KEY — never the key itself, so a
    cookie leak can't be replayed as the device Bearer token (which is the
    raw API_KEY). No DB row: validate by recomputing and constant-time
    comparing. Rotating API_KEY invalidates every outstanding cookie.
    """
    return hmac.new(
        API_KEY.encode("utf-8"), b"epepper-web-session", hashlib.sha256
    ).hexdigest()


def cookie_is_valid(cookie: str) -> bool:
    """Constant-time check that `cookie` matches the derived session value."""
    return bool(cookie) and hmac.compare_digest(cookie, session_cookie_value())


def _is_authed(request: Request) -> bool:
    return cookie_is_valid(request.cookies.get(COOKIE_NAME, ""))


def _require_auth(request: Request) -> None:
    """Redirect to /app/login for unauthed requests.

    HTMX requests get HX-Redirect so the swap target isn't replaced with the
    login page; full-page navs get a 303.
    """
    if _is_authed(request):
        return
    if request.headers.get("HX-Request") == "true":
        raise HTTPException(401, headers={"HX-Redirect": "/app/login"})
    raise HTTPException(303, headers={"Location": "/app/login"})


def _fmt_saved(ts: int | None) -> str:
    return humanize_date(ts)


def _fmt_servings(raw) -> str | None:
    """Render the recipe's servings string as a glanceable "Serves N".

    `servings` is free-form across sources ("4", "4 servings", "Pour 4
    personnes", "4-6"). The web UI is English regardless of recipe
    language, so we pull the first integer (or range) and prefix it with
    "Serves"; when there's no number to anchor on we fall back to the raw
    string. None/empty yields None so the template can drop the line.
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = re.search(r"\d+(?:\s*[–-]\s*\d+)?", s)
    if m:
        return f"Serves {m.group(0).replace(' ', '')}"
    return s


def _ingredients(recipe: dict) -> list[str]:
    ings = recipe.get("ingredients") or []
    return [str(i) for i in ings if i]


def _instruction_groups(recipe: dict) -> list[dict]:
    """Group flat instructions into sections so the template can render
    properly nested <h3> + <ol> blocks (Jinja autoescaping rules out
    inlining tags in strings).

    Input: parser flattens recipes to a list of {"type": "heading"|"step",
    "text": ...}. Steps without a preceding heading land in an initial
    headless group.

    The dedupe / cleanup rules (drop empty items, collapse duplicate
    headings, collapse heading-only runs) live in
    `processing.recipes.normalize_recipe_for_render` so the BMP renderer
    and this one share a single source of truth. Kept here as
    defense-in-depth for any legacy `parsed_json` rows written before the
    upsert path started normalizing on write.
    """
    items = normalize_recipe_for_render(recipe).get("instructions") or []
    groups: list[dict] = [{"heading": None, "steps": []}]
    for item in items:
        if item.get("type") == "heading":
            text = item.get("text", "")
            if not groups[-1]["steps"] and groups[-1]["heading"] is not None:
                groups[-1]["heading"] = text
            else:
                groups.append({"heading": text, "steps": []})
        else:
            groups[-1]["steps"].append(item.get("text", ""))
    # Drop the empty leading group if nothing landed in it.
    if not groups[0]["steps"] and groups[0]["heading"] is None and len(groups) > 1:
        groups = groups[1:]
    return groups


def _context_globals(request: Request) -> dict:
    return {
        "request": request,
        "fmt_saved": _fmt_saved,
        "fmt_servings": _fmt_servings,
        "ingredients": _ingredients,
        "instruction_groups": _instruction_groups,
        "source_name": source_name,
        "saved_count": library.count_saved(),
    }


router = APIRouter(prefix="/app", tags=["web"])


# --- Auth -------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None):
    if _is_authed(request):
        return RedirectResponse("/app/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": error},
    )


@router.post("/login")
async def login_submit(request: Request, api_key: str = Form(...)):
    if not secrets.compare_digest(api_key, API_KEY):
        return RedirectResponse("/app/login?error=1", status_code=303)
    # Cookie carries an HMAC of the API key (not the key itself), so a cookie
    # leak doesn't hand over the device Bearer credential.
    resp = RedirectResponse("/app/", status_code=303)
    resp.set_cookie(
        COOKIE_NAME,
        session_cookie_value(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return resp


# --- Browse / search --------------------------------------------------------


_PAGE_SIZE = 20


def _sanitize_source(source: str | None) -> str | None:
    """Lowercase + strip. Empty/whitespace → None. SQL uses parameter
    binding so the value can't escape the WHERE clause."""
    if not source:
        return None
    s = source.strip().lower()
    return s or None


_TAG_TOKEN_RE = __import__("re").compile(r"^\w+$", __import__("re").UNICODE)


def _sanitize_tag(tag: str | None) -> str | None:
    """Strip the leading `#` if present, lowercase, accept word chars only.

    Bound into SQL via parameter binding, but the strict character class
    also keeps random punctuation out of the tag dropdown's selected state.
    """
    if not tag:
        return None
    s = tag.strip().lstrip("#").lower()
    if not s or not _TAG_TOKEN_RE.match(s):
        return None
    return s



def _bucket_recipes(
    recipes: list[dict],
) -> list[tuple[str, str, list[dict]]]:
    """Group recipes into time-tiers (most-recently-cooked first).

    Returns a list of (slug, label, rows) tuples in display order;
    empty tiers are dropped. The template just iterates — no logic.
    """
    this_week: list[dict] = []
    this_month: list[dict] = []
    this_year: list[dict] = []
    older: list[dict] = []
    never: list[dict] = []
    for r in recipes:
        if r.get("last_displayed_at") is None:
            never.append(r)
            continue
        phrase = humanize_date(r.get("last_displayed_at"))
        if (
            "min ago" in phrase or "h ago" in phrase or "just now" in phrase
            or phrase == "yesterday" or "days ago" in phrase
        ):
            this_week.append(r)
        elif phrase == "last week" or "weeks ago" in phrase:
            this_month.append(r)
        elif phrase == "last month" or "months ago" in phrase:
            this_year.append(r)
        else:
            older.append(r)
    tiers = [
        ("this-week", "This week", this_week),
        ("this-month", "This month", this_month),
        ("this-year", "Earlier this year", this_year),
        ("older", "Older", older),
        ("never", "Never cooked", never),
    ]
    return [t for t in tiers if t[2]]


_TIER_SLUG_RE = __import__("re").compile(r"^[a-z0-9-]{1,64}$")


def _sanitize_tier(tier: str | None) -> str:
    """Whitelist what we accept in the `prev_tier` query param so a
    crafted value can't slip into the template output unescaped or
    break the matching logic. Tier slugs are lowercase + digits +
    hyphens only (see _bucket_by_recency — fixed strings)."""
    if not tier:
        return ""
    s = tier.strip().lower()
    return s if _TIER_SLUG_RE.match(s) else ""


def _list_context(
    request: Request,
    q: str,
    offset: int,
    source: str | None,
    tag: str | None,
    prev_tier: str = "",
) -> dict:
    rows = library.list_recipes(
        offset=offset,
        limit=_PAGE_SIZE + 1,
        query=q or None,
        source=source,
        tag=tag,
    )
    has_more = len(rows) > _PAGE_SIZE
    rows = rows[:_PAGE_SIZE]
    tiers = _bucket_recipes(rows)
    return {
        "request": request,
        "recipes": rows,
        "tiers": tiers,
        # The last rendered tier slug travels with the load-more URL so
        # the next batch's first tier suppresses its <h2> when it would
        # otherwise duplicate the heading the client already painted.
        "prev_tier": prev_tier,
        "last_tier": tiers[-1][0] if tiers else "",
        "q": q,
        "source": source or "",
        "tag": tag or "",
        "sources": library.list_sources(),
        "tags": library.list_tags(),
        "offset": offset,
        "next_offset": offset + _PAGE_SIZE,
        "has_more": has_more,
        "fmt_saved": _fmt_saved,
        "source_name": source_name,
        "current_recipe_id": display_state.get().get("recipe_id"),
    }


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    q: str = "",
    offset: int = 0,
    source: str | None = None,
    tag: str | None = None,
):
    _require_auth(request)
    source = _sanitize_source(source)
    tag = _sanitize_tag(tag)
    ctx = _context_globals(request)
    ctx.update(_list_context(request, q, offset, source, tag))
    return templates.TemplateResponse(request, "index.html", ctx)


@router.get("/_search", response_class=HTMLResponse)
async def search_partial(
    request: Request,
    q: str = "",
    offset: int = 0,
    source: str | None = None,
    tag: str | None = None,
    prev_tier: str | None = None,
):
    """HTMX partial — re-renders only the result list as the search box,
    source, or tag filter changes, or the Load more button is tapped.

    `prev_tier` is the slug of the last tier the previous batch painted;
    when this batch's first tier matches, we skip its <h2> so the
    paginated stream doesn't double-print the heading on a tier that
    spans multiple pages.
    """
    _require_auth(request)
    source = _sanitize_source(source)
    tag = _sanitize_tag(tag)
    ctx = _list_context(
        request, q, offset, source, tag,
        prev_tier=_sanitize_tier(prev_tier),
    )
    ctx["is_partial"] = offset == 0
    ctx["saved_count"] = library.count_saved()
    response = templates.TemplateResponse(request, "_list.html", ctx)
    if offset == 0:
        response.headers["HX-Replace-Url"] = _user_facing_url(q, source, tag)
    return response


def _user_facing_url(q: str, source: str | None, tag: str | None) -> str:
    """Build the URL the browser bar should reflect for an active filter set."""
    from urllib.parse import urlencode
    params = [(k, v) for k, v in [("q", q), ("source", source), ("tag", tag)] if v]
    return "/app/" + ("?" + urlencode(params) if params else "")


# --- Add recipe -------------------------------------------------------------


def _filename_hint(filename: str | None) -> str | None:
    """Turn an upload filename into a usable LLM context hint, or None.

    Strips the extension, turns `_`/`-` separators into spaces and trims.
    Returns the cleaned name if non-empty, else None. Generic camera /
    screenshot patterns ("IMG_1234", "Screenshot 2025-…") survive as noise —
    we let the LLM ignore them rather than maintain heuristic gating.
    """
    if not filename:
        return None
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in name:
        name = name.rsplit(".", 1)[0]
    cleaned = " ".join(name.replace("_", " ").replace("-", " ").split())
    return cleaned or None


def _hx_redirect(url: str) -> Response:
    """200 OK with HX-Redirect — HTMX swaps the whole window to `url`.

    Used instead of a 303 because HTMX swallows redirects by default; the
    HX-Redirect header is the documented escape hatch.
    """
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = url
    return resp


def _add_error(request: Request, message: str) -> HTMLResponse:
    """Render a small error fragment back into the add page's #add-result.

    Backtick-delimited tokens in `message` (e.g. `` `.json` ``) become
    <code> spans. Everything else is HTML-escaped first, so the message
    can't smuggle in markup — only the bounded backtick rewrite is
    promoted to safe HTML.
    """
    import re
    from html import escape
    from markupsafe import Markup
    rendered = re.sub(r"`([^`]+)`", r"<code>\1</code>", escape(message))
    return templates.TemplateResponse(
        request, "_add_error.html",
        {"request": request, "message": Markup(rendered)},
        status_code=400,
    )


@router.get("/add", response_class=HTMLResponse)
async def add_page(request: Request):
    _require_auth(request)
    return templates.TemplateResponse(request, "add.html", _context_globals(request))


@router.post("/add/url", response_class=HTMLResponse)
async def add_url(request: Request, url: str = Form(...)):
    """URL paste — adds the recipe to the repertoire, without pushing to the panel.

    Dedupes via find_by_url so a re-pasted URL just lands the user back on
    its detail page. The explicit Display button on the detail page is
    what actually sends a recipe to the panel — so `last_displayed_at`
    only moves when the user really wants the recipe shown.
    """
    _require_auth(request)
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return _add_error(request, "Not a `http(s)://` URL.")

    existing = library.find_by_url(url)
    if existing is not None:
        log.info("Web add (existing URL): id=%d url=%s", existing["id"], url)
        return _hx_redirect(f"/app/recipes/{existing['id']}")

    try:
        result = await ingest_recipe(url, push=False, persist=True)
    except IngestError:
        return _add_error(request, "Couldn't parse a recipe from that URL.")
    log.info(
        "Web add (URL): id=%s title=%r url=%s",
        result["recipe_id"], result["recipe"].get("title"), url,
    )
    return _hx_redirect(f"/app/recipes/{result['recipe_id']}")


@router.post("/add/file", response_class=HTMLResponse)
async def add_file(request: Request, file: UploadFile = File(...)):
    """Single upload endpoint — OCR a recipe photo into the library.

    Mirrors the URL-add path: the photo is OCR'd via the configured LLM,
    saved to the library, and the user lands on the recipe detail page.
    The panel is not touched — Display on the detail page is the
    explicit "push to panel" action.
    """
    _require_auth(request)
    ct = (file.content_type or "").lower()
    name = (file.filename or "").lower()
    is_image = ct.startswith("image/") or name.endswith(
        (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".gif")
    )

    if is_image:
        return await _add_photo_bytes(request, file)
    return _add_error(request, "Pick an image file (JPG, PNG, WebP, HEIC).")


async def _add_photo_bytes(request: Request, file: UploadFile) -> HTMLResponse:
    raw = await file.read()
    if len(raw) > _PHOTO_MAX_BYTES:
        return _add_error(
            request,
            f"Image too large (limit {PHOTO_MAX_MB} MB). Try a lower-resolution "
            "shot, or send it via the Telegram bot (which downscales server-side).",
        )
    hint = _filename_hint(file.filename)
    log.info("Web add (photo OCR): %d bytes hint=%r", len(raw), hint or "")
    # ingest_recipe handles parse → translate → upsert → save. We still log
    # an "existing OCR" hit separately so the deduped-photo path is
    # observable in the access log (find_by_url runs inside ingest_recipe
    # but doesn't surface the "already had this" signal).
    try:
        result = await ingest_recipe(raw, push=False, persist=True, hint=hint)
    except IngestError:
        return _add_error(request, "Couldn't read a recipe from that photo.")
    log.info(
        "Web add (photo OCR): id=%s title=%r url=%s",
        result["recipe_id"], result["recipe"].get("title"), result["url"],
    )
    return _hx_redirect(f"/app/recipes/{result['recipe_id']}")


# --- Status page -----------------------------------------------------------


def _status_ctx(request: Request) -> dict:
    """Build the data dict the status templates render against.

    Both the full page and the HTMX auto-refresh partial pull from here so
    they can't drift apart.
    """
    display = display_state.get()
    device = device_telemetry.get_device_status()
    pct = battery_pct(device["battery_mv"]) if device.get("battery_mv") else None
    # Latest firmware version published by CI (rsynced into the bind-mounted
    # firmware/ dir). None when no firmware has been pushed yet, in which
    # case the template skips the "update pending" chip.
    firmware_server_version: int | None = None
    try:
        version_file = Path("/app/firmware/version.txt")
        if version_file.exists():
            firmware_server_version = int(version_file.read_text().strip())
    except (ValueError, OSError):
        pass
    overdue_s = (
        int(time.time()) - device["last_seen"]
        if device.get("last_seen") else 0
    )
    is_overdue = (
        bool(device.get("last_seen"))
        and overdue_s > device_telemetry.STALE_HEARTBEAT_S
    )
    is_low_battery = (
        device.get("battery_mv", 0) > 0
        and device["battery_mv"] < device_telemetry.LOW_BATTERY_MV
    )
    # Tomorrow's preview — mirrors what the midnight scheduler will push:
    #   1. If an anniversary candidate lands on tomorrow's MM-DD, show that.
    #   2. Else, surface the pre-fetched Fooby pick from fooby_cache (set
    #      by the previous midnight tick or the startup prefetch). The
    #      template links the title to the source URL and labels it
    #      "inspiration from Fooby".
    #   3. Else, generic "Fooby will play" hint (cache missing / stale —
    #      a first deploy that hasn't reached its first midnight yet,
    #      typically).
    now_local = datetime.now(TZ)
    tomorrow = now_local + timedelta(days=1)
    next_anniv = library.pick_anniversary_recipe(
        tomorrow.strftime("%m-%d"), tomorrow.year
    )
    next_anniv_years_ago: int | None = None
    if next_anniv and next_anniv.get("last_displayed_at"):
        cooked_year = datetime.fromtimestamp(
            next_anniv["last_displayed_at"], TZ
        ).year
        next_anniv_years_ago = tomorrow.year - cooked_year
    fooby_preview: dict | None = None
    if next_anniv is None:
        cached = fooby_cache.get()
        if cached and cached.get("for_date") == tomorrow.date().isoformat():
            fooby_preview = cached
    return {
        "request": request,
        "display": display,
        "device": device,
        "battery_pct": pct,
        "is_low_battery": is_low_battery,
        "is_overdue": is_overdue,
        "humanize_ago": humanize_ago,
        "rssi_quality": rssi_quality,
        "fmt_updated": humanize_date(display.get("updated_at")),
        "backup_enabled": backup.is_enabled(),
        "last_backup_at": backup.get_last_backup_at(),
        "next_anniversary": next_anniv,
        "next_anniversary_years_ago": next_anniv_years_ago,
        "next_anniversary_date": format_long_date(tomorrow),
        "fooby_preview": fooby_preview,
        "firmware_server_version": firmware_server_version,
        # _context_globals only fires on the full /status page render; the
        # 30 s HTMX partial calls this directly, so re-include the bits the
        # status partial needs (saved_count for the Library card, source_name
        # for the "from X" chip on the Display card). Without these the
        # partial silently renders blank values after the first refresh.
        "saved_count": library.count_saved(),
        "source_name": source_name,
    }


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    _require_auth(request)
    ctx = _context_globals(request)
    ctx.update(_status_ctx(request))
    return templates.TemplateResponse(request, "status.html", ctx)


@router.get("/_status", response_class=HTMLResponse)
async def status_partial(request: Request):
    """HTMX partial — re-rendered every 30s by the status page to keep the
    live device readings + display preview fresh without a full reload."""
    _require_auth(request)
    return templates.TemplateResponse(request, "_status_body.html", _status_ctx(request))


@router.post("/display/clear", response_class=HTMLResponse)
async def web_display_clear(request: Request):
    """Clear the display from the status page — same effect as the bot's /clear."""
    _require_auth(request)
    display_state.clear()
    log.info("Web cleared display")
    return _hx_redirect("/app/status?cleared=1")


# --- Recipe detail ---------------------------------------------------------


@router.get("/recipes/{recipe_id}", response_class=HTMLResponse)
async def recipe_detail(request: Request, recipe_id: int):
    _require_auth(request)
    row = library.get_recipe(recipe_id)
    if row is None:
        raise HTTPException(404)
    ctx = _context_globals(request)
    ctx.update({"r": row, "all_tags": library.list_tags()})
    return templates.TemplateResponse(request, "recipe.html", ctx)


@router.post("/recipes/{recipe_id}/tags", response_class=HTMLResponse)
async def tags_save(request: Request, recipe_id: int, tags: str = Form(default="")):
    _require_auth(request)
    row = library.get_recipe(recipe_id)
    if row is None:
        raise HTTPException(404)
    parsed = [t.strip().lower() for t in tags.split(",") if t.strip()]
    library.set_tags(recipe_id, parsed)
    row = library.get_recipe(recipe_id)
    return templates.TemplateResponse(
        request, "_tags.html", {"r": row, "all_tags": library.list_tags()},
    )


# --- Push to display -------------------------------------------------------


@router.post("/recipes/{recipe_id}/push", response_class=HTMLResponse)
async def push_recipe(request: Request, recipe_id: int):
    _require_auth(request)
    row = library.get_recipe(recipe_id)
    if row is None:
        raise HTTPException(404)
    if not push_recipe_to_display(row):
        return templates.TemplateResponse(
            request, "_toast.html",
            {"message": "Couldn't render that recipe to the display."},
            status_code=500,
        )
    log.info("Web push to display: id=%d title=%r", row["id"], row["title"])
    return templates.TemplateResponse(
        request, "_toast.html",
        {"message": f"Pushed “{row['title']}” to the display."},
    )


# --- Delete -----------------------------------------------------------------


@router.delete("/recipes/{recipe_id}", response_class=HTMLResponse)
async def delete_recipe(request: Request, recipe_id: int):
    """Soft-delete a recipe (set `deleted_at`; the row stays recoverable
    from a backup)."""
    _require_auth(request)
    row = library.get_recipe(recipe_id)
    if row is None:
        raise HTTPException(404)
    if not library.delete_recipe(recipe_id):
        raise HTTPException(404)
    log.info("Web deleted recipe id=%d title=%r", recipe_id, row["title"])
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = "/app/"
    return resp


# --- Flash device (OTA recovery) --------------------------------------------

# Browser-based recovery path for when OTA can't reach the device (bad build,
# bricked partition). Serves the full merged image to ESP Web Tools over Web
# Serial. Session-gated: the merged .bin has WiFi creds + API key baked in,
# same as the OTA app image. Files are rsynced in by the firmware CI job.
_FIRMWARE_DIR = Path("/app/firmware")
_FLASH_FILES = {"manifest.json", "epepper-merged.bin"}


@router.get("/flash", response_class=HTMLResponse)
async def flash_page(request: Request):
    _require_auth(request)
    manifest_present = (_FIRMWARE_DIR / "manifest.json").exists()
    return templates.TemplateResponse(
        request,
        "flash.html",
        {**_context_globals(request), "manifest_present": manifest_present},
    )


@router.get("/flash/{filename}")
async def flash_file(request: Request, filename: str):
    _require_auth(request)
    if filename not in _FLASH_FILES:
        raise HTTPException(404)
    path = _FIRMWARE_DIR / filename
    if not path.exists():
        raise HTTPException(404, "firmware not yet published — wait for the next CI run")
    return FileResponse(
        path,
        media_type=(
            "application/json" if filename.endswith(".json")
            else "application/octet-stream"
        ),
    )


