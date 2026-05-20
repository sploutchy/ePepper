"""Web UI for browsing, searching, and managing the recipe library.

Server-rendered HTML + HTMX partials. Cookie-based session auth (the cookie
stores the same API_KEY the device uses; httpOnly + Secure + SameSite=Lax).
Routes live under /app/ to keep the existing device-facing endpoints clean.
"""

import json
import logging
import secrets
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import backup
import display_state
import library
from config import API_KEY
from display_push import push_recipe_to_display
from processing.images import process_photo
from processing.jsonld import parse_recipe_jsonld, synthetic_url
from processing.recipes import process_recipe_url
from status_helpers import battery_pct, humanize_ago, rssi_quality

log = logging.getLogger(__name__)

# Hard cap on uploaded files. Schema.org Recipe payloads are typically a few
# KB; images uploaded for the display max out around 1–2 MB after browser-side
# JPEG re-encode. 8 MB leaves headroom while still rejecting accidental drops.
_JSON_MAX_BYTES = 256 * 1024
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
COOKIE_MAX_AGE = 365 * 24 * 3600  # one year

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
templates = Jinja2Templates(directory=str(_WEB_DIR / "templates"))


def _is_authed(request: Request) -> bool:
    cookie = request.cookies.get(COOKIE_NAME, "")
    return bool(cookie) and secrets.compare_digest(cookie, API_KEY)


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


def _stars(rating: int | None) -> str:
    return ("⭐" * rating) if rating else ""


def _fmt_saved(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


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


def _source_name(url: str | None) -> str | None:
    """Humanize a recipe URL's host: 'fooby.ch' → 'Fooby'.

    Returns None for missing or synthetic (jsonld:...) URLs so the template
    can hide the source line entirely.
    """
    if not url or url.startswith("jsonld:"):
        return None
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 2:
        return parts[-2].capitalize()
    return host or None


def _context_globals(request: Request) -> dict:
    return {
        "request": request,
        "stars": _stars,
        "fmt_saved": _fmt_saved,
        "ingredients": _ingredients,
        "instruction_groups": _instruction_groups,
        "source_name": _source_name,
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
    resp = RedirectResponse("/app/", status_code=303)
    resp.set_cookie(
        COOKIE_NAME,
        API_KEY,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return resp


@router.post("/logout")
async def logout(request: Request):
    resp = RedirectResponse("/app/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# --- Browse / search --------------------------------------------------------


_PAGE_SIZE = 20


_VALID_SORTS = {"rated", "rated_low", "oldest", "recent"}


def _sanitize_sort(sort: str | None) -> str | None:
    """Ignore anything not in the whitelist — list_recipes splices the sort
    key into SQL via a separate dict, but we still drop garbage here so the
    template's `selected` markers reflect reality."""
    return sort if sort in _VALID_SORTS else None


def _sanitize_min_rating(min_rating: str | None) -> int | None:
    """Tolerate an empty string from the "All ratings" select / pagination
    template, which FastAPI would otherwise 422 on for an `int | None` param."""
    if not min_rating:
        return None
    try:
        n = int(min_rating)
    except ValueError:
        return None
    return n if 1 <= n <= 5 else None


def _list_context(
    request: Request,
    q: str,
    offset: int,
    sort: str | None,
    min_rating: int | None,
) -> dict:
    rows = library.list_recipes(
        offset=offset,
        limit=_PAGE_SIZE + 1,
        query=q or None,
        sort=sort,
        min_rating=min_rating,
    )
    has_more = len(rows) > _PAGE_SIZE
    rows = rows[:_PAGE_SIZE]
    return {
        "request": request,
        "recipes": rows,
        "q": q,
        "sort": sort or "",
        "min_rating": min_rating,
        "offset": offset,
        "next_offset": offset + _PAGE_SIZE,
        "has_more": has_more,
        "stars": _stars,
        "fmt_saved": _fmt_saved,
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
    min_rating: str | None = None,
):
    _require_auth(request)
    sort = _sanitize_sort(sort)
    parsed_min_rating = _sanitize_min_rating(min_rating)
    ctx = _context_globals(request)
    ctx.update(_list_context(request, q, offset, sort, parsed_min_rating))
    return templates.TemplateResponse(request, "index.html", ctx)


@router.get("/_search", response_class=HTMLResponse)
async def search_partial(
    request: Request,
    q: str = "",
    offset: int = 0,
    sort: str | None = None,
    min_rating: str | None = None,
):
    """HTMX partial — re-renders only the result list as the search box,
    sort, or rating filter changes, or the Load more button is tapped."""
    _require_auth(request)
    sort = _sanitize_sort(sort)
    parsed_min_rating = _sanitize_min_rating(min_rating)
    ctx = _list_context(request, q, offset, sort, parsed_min_rating)
    template = "_list_append.html" if offset > 0 else "_list.html"
    return templates.TemplateResponse(request, template, ctx)


# --- Add recipe -------------------------------------------------------------


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
    """URL paste — mirrors the bot's on_text URL flow.

    Dedupes via find_by_url so a re-pasted URL just re-pushes the cached
    parse instead of re-fetching the site. New URLs are upserted before the
    detail-page redirect so the rating widget has a real recipe_id to bind
    to; the row sits with saved_at=NULL and is invisible in the library
    list until the user picks a rating.
    """
    _require_auth(request)
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return _add_error(request, "Not a `http(s)://` URL.")

    existing = library.find_by_url(url)
    if existing is not None:
        push_recipe_to_display(existing)
        log.info("Web add (existing URL): id=%d url=%s", existing["id"], url)
        return _hx_redirect(f"/app/recipes/{existing['id']}")

    recipe = await process_recipe_url(url)
    if recipe is None:
        return _add_error(request, "Couldn't parse a recipe from that URL.")
    recipe_id = library.upsert_recipe(url, recipe)
    push_recipe_to_display(library.get_recipe(recipe_id))
    log.info("Web add (URL): id=%d title=%r url=%s", recipe_id, recipe.get("title"), url)
    return _hx_redirect(f"/app/recipes/{recipe_id}")


@router.post("/add/file", response_class=HTMLResponse)
async def add_file(request: Request, file: UploadFile = File(...)):
    """Single upload endpoint — dispatches by content type / extension.

    Images go through process_photo (resize + dither → display-only,
    not saved). JSON files go through parse_recipe_jsonld (upserted
    into the library, same dedup-by-URL behaviour as the URL ingest
    path).
    """
    _require_auth(request)
    ct = (file.content_type or "").lower()
    name = (file.filename or "").lower()
    is_image = ct.startswith("image/") or name.endswith(
        (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".gif")
    )
    is_json = ct in ("application/json", "application/ld+json") or name.endswith(".json")

    if is_image:
        return await _add_photo_bytes(request, file)
    if is_json:
        return await _add_jsonld_bytes(request, file)
    return _add_error(request, "Pick an image or a `.json` file.")


async def _add_photo_bytes(request: Request, file: UploadFile) -> HTMLResponse:
    raw = await _read_capped(file, _PHOTO_MAX_BYTES)
    if raw is None:
        return _add_error(
            request,
            f"Image too large (limit {_PHOTO_MAX_BYTES // 1024} KB).",
        )
    try:
        img = process_photo(raw)
    except Exception:
        log.exception("Web photo processing failed")
        return _add_error(request, "Couldn't read that image.")
    display_state.set_image(img, content_type="photo")
    log.info("Web add (photo): %d bytes", len(raw))
    return _hx_redirect("/app/status?pushed=image")


async def _add_jsonld_bytes(request: Request, file: UploadFile) -> HTMLResponse:
    raw = await _read_capped(file, _JSON_MAX_BYTES)
    if raw is None:
        return _add_error(
            request,
            f"JSON file too large (limit {_JSON_MAX_BYTES // 1024} KB).",
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return _add_error(request, "Not valid JSON.")
    parsed = parse_recipe_jsonld(data)
    if parsed is None:
        return _add_error(request, "No schema.org `Recipe` in that `.json`.")
    recipe, source_url = parsed
    url = source_url or synthetic_url(recipe)
    existing = library.find_by_url(url)
    if existing is not None:
        push_recipe_to_display(existing)
        log.info("Web add (existing JSON-LD): id=%d", existing["id"])
        return _hx_redirect(f"/app/recipes/{existing['id']}")
    recipe_id = library.upsert_recipe(url, recipe)
    push_recipe_to_display(library.get_recipe(recipe_id))
    log.info("Web add (JSON-LD): id=%d title=%r", recipe_id, recipe.get("title"))
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
    return {
        "request": request,
        "display": display,
        "device": device,
        "battery_pct": pct,
        "is_low_battery": is_low_battery,
        "is_overdue": is_overdue,
        "humanize_ago": humanize_ago,
        "rssi_quality": rssi_quality,
        "fmt_updated": (
            datetime.fromtimestamp(display["updated_at"]).strftime("%Y-%m-%d %H:%M")
            if display.get("updated_at") else "—"
        ),
        "backup_enabled": backup.is_enabled(),
        "last_backup_at": backup.get_last_backup_at(),
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
    ctx.update({
        "r": row,
        "comments": library.get_comments(recipe_id),
    })
    return templates.TemplateResponse(request, "recipe.html", ctx)


def _rating_ctx(request: Request, recipe_id: int, rating: int | None) -> dict:
    return {"request": request, "r_id": recipe_id, "rating": rating, "stars": _stars}


@router.post("/recipes/{recipe_id}/rating", response_class=HTMLResponse)
async def update_rating(request: Request, recipe_id: int, rating: int = Form(...)):
    _require_auth(request)
    if not 1 <= rating <= 5:
        raise HTTPException(400, detail="rating must be 1..5")
    if not library.mark_saved(recipe_id, rating):
        raise HTTPException(404)
    backup.schedule()
    return templates.TemplateResponse(
        request, "_rating.html", _rating_ctx(request, recipe_id, rating)
    )


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
    backup.schedule()
    return templates.TemplateResponse(request, "_comments.html", _comments_ctx(request, recipe_id))


@router.delete("/recipes/{recipe_id}/comments/{comment_id}", response_class=HTMLResponse)
async def delete_comment(request: Request, recipe_id: int, comment_id: int):
    _require_auth(request)
    parent = library.remove_comment(comment_id)
    if parent is None or parent != recipe_id:
        raise HTTPException(404)
    backup.schedule()
    return templates.TemplateResponse(request, "_comments.html", _comments_ctx(request, recipe_id))


# --- Push to display -------------------------------------------------------


@router.post("/recipes/{recipe_id}/push", response_class=HTMLResponse)
async def push_recipe(request: Request, recipe_id: int):
    _require_auth(request)
    row = library.get_recipe(recipe_id)
    if row is None:
        raise HTTPException(404)
    push_recipe_to_display(row)
    log.info("Web push to display: id=%d title=%r", row["id"], row["title"])
    return templates.TemplateResponse(
        request, "_toast.html",
        {"message": f"Pushed “{row['title']}” to the display."},
    )


# --- Delete -----------------------------------------------------------------


@router.delete("/recipes/{recipe_id}", response_class=HTMLResponse)
async def delete_recipe(request: Request, recipe_id: int):
    """Soft-delete a recipe. No restore path — deleted rows are recoverable
    from the Telegram DB backups if you really need them."""
    _require_auth(request)
    row = library.get_recipe(recipe_id)
    if row is None:
        raise HTTPException(404)
    if not library.delete_recipe(recipe_id):
        raise HTTPException(404)
    backup.schedule()
    log.info("Web deleted recipe id=%d title=%r", recipe_id, row["title"])
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = "/app/"
    return resp
