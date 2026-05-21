"""Web UI for browsing, searching, and managing the recipe library.

Server-rendered HTML + HTMX partials. Cookie-based session auth (the cookie
stores the same API_KEY the device uses; httpOnly + Secure + SameSite=Lax).
Routes live under /app/ to keep the existing device-facing endpoints clean.
"""

import logging
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import backup
import display_state
import fooby_cache
import library
from library.db import SESSION_DURATION_S
from config import API_KEY, LLM_API_KEY, LLM_API_URL, TZ
from display_push import push_recipe_to_display
from processing.recipes import (
    process_recipe_image,
    process_recipe_url,
    translate_for_search,
)
from status_helpers import battery_pct, humanize_ago, humanize_date, rssi_quality, source_name

log = logging.getLogger(__name__)

# Hard cap on photo uploads. Phone shots after browser-side JPEG re-encode
# are typically 1-3 MB; 8 MB leaves headroom while still rejecting
# accidental drops.
_PHOTO_MAX_BYTES = 8 * 1024 * 1024

# Stream-read chunk size for the size-capped upload reader.
_UPLOAD_CHUNK_BYTES = 64 * 1024


async def _read_capped(file: UploadFile, cap: int) -> bytes | None:
    """Read an UploadFile into memory, bailing out if it exceeds `cap` bytes.

    Returns the bytes on success, or None when the upload is over the cap.
    Streamed so a 10 GB file doesn't OOM the process before the size check.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)

COOKIE_NAME = "epepper_auth"
# Match the server-side session lifetime — the cookie outliving the session
# row would just produce silent re-login redirects.
COOKIE_MAX_AGE = SESSION_DURATION_S

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
templates = Jinja2Templates(directory=str(_WEB_DIR / "templates"))


def _is_authed(request: Request) -> bool:
    return library.validate_session(request.cookies.get(COOKIE_NAME, ""))


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
    """
    items = recipe.get("instructions") or []
    groups: list[dict] = [{"heading": None, "steps": []}]
    for item in items:
        if isinstance(item, dict) and item.get("type") == "heading":
            text = (item.get("text") or "").strip()
            if not text:
                continue
            groups.append({"heading": text, "steps": []})
        else:
            text = (
                item.get("text") if isinstance(item, dict) else str(item)
            ) or ""
            text = text.strip()
            if text:
                groups[-1]["steps"].append(text)
    # Drop the empty leading group if nothing landed in it.
    if not groups[0]["steps"] and groups[0]["heading"] is None and len(groups) > 1:
        groups = groups[1:]
    return groups


def _context_globals(request: Request) -> dict:
    return {
        "request": request,
        "fmt_saved": _fmt_saved,
        "ingredients": _ingredients,
        "instruction_groups": _instruction_groups,
        "source_name": source_name,
        "saved_count": library.count_saved(),
    }


router = APIRouter(prefix="/app", tags=["web"])


# --- PWA --------------------------------------------------------------------


@router.get("/sw.js", include_in_schema=False)
async def service_worker():
    """Serve the service worker at /app/sw.js so its default registration
    scope is /app/ — couldn't be served from /app/static/ without
    Service-Worker-Allowed header gymnastics. No auth gate: the SW is
    static asset code and the file itself caches only public shell URLs.
    """
    sw_path = _WEB_DIR / "static" / "sw.js"
    return Response(
        content=sw_path.read_bytes(),
        media_type="application/javascript",
        # Disable HTTP caching so SW updates roll out promptly; the
        # browser still re-checks the script on every navigation anyway.
        headers={"Cache-Control": "no-cache"},
    )


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
    # Cookie stores a random session token (not the API key), so a cookie
    # leak doesn't hand over the device credential.
    token = library.create_session()
    resp = RedirectResponse("/app/", status_code=303)
    resp.set_cookie(
        COOKIE_NAME,
        token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return resp


@router.post("/logout")
async def logout(request: Request):
    library.delete_session(request.cookies.get(COOKIE_NAME, ""))
    resp = RedirectResponse("/app/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# --- Browse / search --------------------------------------------------------


_PAGE_SIZE = 20


_VALID_SORTS = {"oldest", "recent", "most_cooked"}


def _sanitize_sort(sort: str | None) -> str | None:
    """Ignore anything not in the whitelist — list_recipes splices the sort
    key into SQL via a separate dict, but we still drop garbage here so the
    template's `selected` markers reflect reality."""
    return sort if sort in _VALID_SORTS else None


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


def _list_context(
    request: Request,
    q: str,
    offset: int,
    sort: str | None,
    source: str | None,
    tag: str | None,
) -> dict:
    rows = library.list_recipes(
        offset=offset,
        limit=_PAGE_SIZE + 1,
        query=q or None,
        sort=sort,
        source=source,
        tag=tag,
    )
    has_more = len(rows) > _PAGE_SIZE
    rows = rows[:_PAGE_SIZE]
    return {
        "request": request,
        "recipes": rows,
        "q": q,
        "sort": sort or "",
        "source": source or "",
        "tag": tag or "",
        "sources": library.list_sources(),
        "tags": library.list_tags(),
        "offset": offset,
        "next_offset": offset + _PAGE_SIZE,
        "has_more": has_more,
        "fmt_saved": _fmt_saved,
        # Helpers for the per-row source chip on the list cards.
        "source_name": source_name,
        # Marks the recipe currently rendered on the e-ink display so the
        # list can flag it. None if the display isn't showing a saved recipe.
        "current_recipe_id": display_state.get().get("recipe_id"),
    }


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    q: str = "",
    offset: int = 0,
    sort: str | None = None,
    source: str | None = None,
    tag: str | None = None,
):
    _require_auth(request)
    sort = _sanitize_sort(sort)
    source = _sanitize_source(source)
    tag = _sanitize_tag(tag)
    ctx = _context_globals(request)
    ctx.update(_list_context(request, q, offset, sort, source, tag))
    return templates.TemplateResponse(request, "index.html", ctx)


@router.get("/_search", response_class=HTMLResponse)
async def search_partial(
    request: Request,
    q: str = "",
    offset: int = 0,
    sort: str | None = None,
    source: str | None = None,
    tag: str | None = None,
):
    """HTMX partial — re-renders only the result list as the search box,
    sort, source, or tag filter changes, or the Load more button is tapped."""
    _require_auth(request)
    sort = _sanitize_sort(sort)
    source = _sanitize_source(source)
    tag = _sanitize_tag(tag)
    ctx = _list_context(request, q, offset, sort, source, tag)
    template = "_list_append.html" if offset > 0 else "_list.html"
    return templates.TemplateResponse(request, template, ctx)


# --- Add recipe -------------------------------------------------------------


def _filename_hint(filename: str | None) -> str | None:
    """Turn an upload filename into a usable LLM context hint, or None.

    Strips the directory prefix and the extension, replaces `_`, `-` and
    `.` separators with spaces. Generic camera / screenshot patterns
    ("IMG_1234", "Screenshot 2025-…", "photo (3)") collapse to noise —
    we keep the cleaned form anyway and let the LLM ignore it, rather
    than maintain a brittle blocklist. Returns None for empty / fully
    numeric / single-token names so we don't waste a hint slot on
    them.
    """
    if not filename:
        return None
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in name:
        name = name.rsplit(".", 1)[0]
    cleaned = " ".join(part for part in name.replace("_", " ").replace("-", " ").split())
    if not cleaned:
        return None
    # Skip if it's just digits (camera-style) or one short opaque word.
    if cleaned.replace(" ", "").isdigit():
        return None
    if " " not in cleaned and len(cleaned) < 6:
        return None
    return cleaned


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
    """URL paste — adds the recipe to the library, without pushing to the panel.

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

    recipe = await process_recipe_url(url)
    if recipe is None:
        return _add_error(request, "Couldn't parse a recipe from that URL.")
    translated = await translate_for_search(recipe)
    recipe_id = library.upsert_recipe(url, recipe, translated_keywords=translated)
    library.save_recipe(recipe_id)
    log.info("Web add (URL): id=%d title=%r url=%s", recipe_id, recipe.get("title"), url)
    return _hx_redirect(f"/app/recipes/{recipe_id}")


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
    raw = await _read_capped(file, _PHOTO_MAX_BYTES)
    if raw is None:
        return _add_error(
            request,
            f"Image too large (limit {_PHOTO_MAX_BYTES // (1024 * 1024)} MB).",
        )
    hint = _filename_hint(file.filename)
    log.info("Web add (photo OCR): %d bytes hint=%r", len(raw), hint or "")
    result = await process_recipe_image(raw, hint=hint)
    if result is None:
        return _add_error(request, "Couldn't read a recipe from that photo.")
    recipe, url = result
    existing = library.find_by_url(url)
    if existing is not None:
        log.info("Web add (existing OCR): id=%d", existing["id"])
        return _hx_redirect(f"/app/recipes/{existing['id']}")
    translated = await translate_for_search(recipe)
    recipe_id = library.upsert_recipe(url, recipe, translated_keywords=translated)
    library.save_recipe(recipe_id)
    log.info(
        "Web add (photo OCR): id=%d title=%r url=%s",
        recipe_id, recipe.get("title"), url,
    )
    return _hx_redirect(f"/app/recipes/{recipe_id}")


# --- Status page -----------------------------------------------------------


def _status_ctx(request: Request) -> dict:
    """Build the data dict the status templates render against.

    Both the full page and the HTMX auto-refresh partial pull from here so
    they can't drift apart.
    """
    display = display_state.get()
    device = display_state.get_device_status()
    pct = battery_pct(device["battery_mv"]) if device.get("battery_mv") else None
    overdue_s = (
        int(time.time()) - device["last_seen"]
        if device.get("last_seen") else 0
    )
    is_overdue = (
        bool(device.get("last_seen"))
        and overdue_s > display_state.STALE_HEARTBEAT_S
    )
    is_low_battery = (
        device.get("battery_mv", 0) > 0
        and device["battery_mv"] < display_state.LOW_BATTERY_MV
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
    # LLM token+cost ledger for the current calendar month (local TZ —
    # matches Infomaniak's billing boundary).
    month_start = int(
        now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    )
    llm = library.llm_month_stats(month_start)
    llm["enabled"] = bool(LLM_API_URL and LLM_API_KEY)
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
        "next_anniversary_date": tomorrow.strftime("%d.%m"),
        "fooby_preview": fooby_preview,
        "llm": llm,
        # _context_globals only fires on the full /status page render; the
        # 30 s HTMX partial calls this directly, so include source_name
        # here too so the Display card can keep rendering "from X".
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


def _change_page(action: str) -> None:
    """Mutate display_state's current page based on a nav action.

    Mirrors the wrap/clamp behaviour of /page/* in api/server.py so the
    web buttons feel identical to the device's physical buttons.
    """
    state = display_state.get()
    total = state["total_pages"]
    current = state["page"]
    if total <= 1:
        return
    if action == "next":
        new_page = current + 1 if current < total else 1
    elif action == "prev":
        new_page = current - 1 if current > 1 else total
    elif action == "first":
        new_page = 1
    elif action == "last":
        new_page = total
    else:
        return
    display_state.set_page(new_page)
    log.info("Web page %s: %d → %d (of %d)", action, current, new_page, total)


@router.post("/display/page/{action}", response_class=HTMLResponse)
async def web_page_nav(request: Request, action: str):
    """Status-page page-navigation controls — mirror the device buttons.

    Returns the freshly-rendered status body so HTMX can swap the preview
    + the page indicator in one round trip.
    """
    _require_auth(request)
    if action not in ("next", "prev", "first", "last"):
        raise HTTPException(404)
    _change_page(action)
    return templates.TemplateResponse(
        request, "_status_body.html", _status_ctx(request)
    )


# --- Recipe detail ---------------------------------------------------------


@router.get("/recipes/{recipe_id}", response_class=HTMLResponse)
async def recipe_detail(request: Request, recipe_id: int):
    _require_auth(request)
    row = library.get_recipe(recipe_id)
    if row is None:
        raise HTTPException(404)
    ctx = _context_globals(request)
    ctx.update({
        "r": row,
        "comments": library.get_comments(recipe_id),
    })
    return templates.TemplateResponse(request, "recipe.html", ctx)


def _comments_ctx(request: Request, recipe_id: int) -> dict:
    return {
        "request": request,
        "r_id": recipe_id,
        "comments": library.get_comments(recipe_id),
    }


@router.post("/recipes/{recipe_id}/comments", response_class=HTMLResponse)
async def add_comment(request: Request, recipe_id: int, body: str = Form(...)):
    _require_auth(request)
    body = body.strip()
    if not body:
        # No-op: just return the existing list so the form stays put.
        return templates.TemplateResponse(request, "_comments.html", _comments_ctx(request, recipe_id))
    if library.get_recipe(recipe_id) is None:
        raise HTTPException(404)
    library.add_comment(recipe_id, body)
    return templates.TemplateResponse(request, "_comments.html", _comments_ctx(request, recipe_id))


@router.delete("/recipes/{recipe_id}/comments/{comment_id}", response_class=HTMLResponse)
async def delete_comment(request: Request, recipe_id: int, comment_id: int):
    _require_auth(request)
    parent = library.remove_comment(comment_id)
    if parent is None or parent != recipe_id:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "_comments.html", _comments_ctx(request, recipe_id))


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
async def delete_recipe(request: Request, recipe_id: int, hard: int = 0):
    """Delete a recipe.

    Default: soft-delete (set `deleted_at`; row is recoverable from a
    backup). `?hard=1` (the shift-click affordance from the recipe
    detail page): wipe the row outright — comments cascade away, the
    FTS entry is dropped, and the next Telegram backup snapshot will
    no longer carry it.
    """
    _require_auth(request)
    row = library.get_recipe(recipe_id)
    if row is None:
        raise HTTPException(404)
    if hard:
        if not library.hard_delete_recipe(recipe_id):
            raise HTTPException(404)
        log.info("Web HARD-deleted recipe id=%d title=%r", recipe_id, row["title"])
    else:
        if not library.delete_recipe(recipe_id):
            raise HTTPException(404)
        log.info("Web deleted recipe id=%d title=%r", recipe_id, row["title"])
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = "/app/"
    return resp
