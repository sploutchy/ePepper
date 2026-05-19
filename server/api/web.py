"""Web UI for browsing, searching, and managing the recipe library.

Server-rendered HTML + HTMX partials. Cookie-based session auth (the cookie
stores the same API_KEY the device uses; httpOnly + Secure + SameSite=Lax).
Routes live under /app/ to keep the existing device-facing endpoints clean.
"""

import logging
import os
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import backup
import library
from display_push import push_recipe_to_display

log = logging.getLogger(__name__)

API_KEY = os.environ.get("API_KEY", "")
COOKIE_NAME = "epepper_auth"
COOKIE_MAX_AGE = 365 * 24 * 3600  # one year

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
templates = Jinja2Templates(directory=str(_WEB_DIR / "templates"))


def _is_authed(request: Request) -> bool:
    if not API_KEY:
        return True
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


def _instructions(recipe: dict) -> list[dict]:
    """Flatten schema.org HowToStep / HowToSection into a uniform list."""
    items = recipe.get("instructions") or []
    out: list[dict] = []
    for item in items:
        if isinstance(item, dict) and item.get("type") == "section":
            out.append({"type": "heading", "text": item.get("name") or ""})
            for sub in item.get("items") or []:
                if isinstance(sub, dict):
                    out.append({"type": "step", "text": sub.get("text") or ""})
                else:
                    out.append({"type": "step", "text": str(sub)})
        elif isinstance(item, dict):
            out.append({"type": "step", "text": item.get("text") or ""})
        else:
            out.append({"type": "step", "text": str(item)})
    return out


def _context_globals(request: Request) -> dict:
    return {
        "request": request,
        "stars": _stars,
        "fmt_saved": _fmt_saved,
        "ingredients": _ingredients,
        "instructions": _instructions,
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
    if not API_KEY or not secrets.compare_digest(api_key, API_KEY):
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


def _list_context(request: Request, q: str, offset: int) -> dict:
    rows = library.list_recipes(offset=offset, limit=_PAGE_SIZE + 1, query=q or None)
    has_more = len(rows) > _PAGE_SIZE
    rows = rows[:_PAGE_SIZE]
    return {
        "request": request,
        "recipes": rows,
        "q": q,
        "offset": offset,
        "next_offset": offset + _PAGE_SIZE,
        "has_more": has_more,
        "stars": _stars,
        "fmt_saved": _fmt_saved,
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, q: str = "", offset: int = 0):
    _require_auth(request)
    ctx = _context_globals(request)
    ctx.update(_list_context(request, q, offset))
    return templates.TemplateResponse(request, "index.html", ctx)


@router.get("/_search", response_class=HTMLResponse)
async def search_partial(request: Request, q: str = "", offset: int = 0):
    """HTMX partial — re-renders only the result list as the search box changes
    or the Load more button is tapped."""
    _require_auth(request)
    ctx = _list_context(request, q, offset)
    template = "_list_append.html" if offset > 0 else "_list.html"
    return templates.TemplateResponse(request, template, ctx)


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


# --- Push to panel ---------------------------------------------------------


@router.post("/recipes/{recipe_id}/push", response_class=HTMLResponse)
async def push_recipe(request: Request, recipe_id: int):
    _require_auth(request)
    row = library.get_recipe(recipe_id)
    if row is None:
        raise HTTPException(404)
    push_recipe_to_display(row)
    log.info("Web push to panel: id=%d title=%r", row["id"], row["title"])
    return templates.TemplateResponse(
        request, "_toast.html",
        {"message": f"Pushed “{row['title']}” to the panel."},
    )


# --- Soft delete + undo ----------------------------------------------------


@router.delete("/recipes/{recipe_id}", response_class=HTMLResponse)
async def delete_recipe(request: Request, recipe_id: int):
    _require_auth(request)
    row = library.get_recipe(recipe_id)
    if row is None:
        raise HTTPException(404)
    if not library.delete_recipe(recipe_id):
        raise HTTPException(404)
    backup.schedule()
    log.info("Web soft-deleted recipe id=%d title=%r", recipe_id, row["title"])
    # HX-Redirect tells HTMX to navigate the browser; the source page (detail)
    # is gone so we send the user back to the index where the undo toast is
    # rendered with the deleted recipe's id baked in.
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = f"/app/?undo={recipe_id}&undo_title={row['title']}"
    return resp


@router.post("/recipes/{recipe_id}/restore", response_class=HTMLResponse)
async def restore_recipe(request: Request, recipe_id: int):
    _require_auth(request)
    restored = library.restore_recipe(recipe_id)
    if restored is None:
        raise HTTPException(404)
    backup.schedule()
    log.info("Web restored recipe id=%d title=%r", recipe_id, restored["title"])
    return templates.TemplateResponse(
        request, "_toast.html",
        {"message": f"Restored “{restored['title']}”."},
    )
