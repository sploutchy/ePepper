"""FastAPI server — serves images to the ESP32 display."""

import logging
import os
import secrets

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

import display_state

log = logging.getLogger(__name__)
app = FastAPI(title="ePepper", version="0.1.0")

API_KEY = os.environ.get("API_KEY", "")


def _check_api_key(request: Request) -> bool:
    """Validate API key from Authorization header or query param."""
    if not API_KEY:
        return True  # no key configured = open (dev mode)
    # Check Bearer token
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and secrets.compare_digest(auth[7:], API_KEY):
        return True
    # Check query param fallback (for browser testing)
    if secrets.compare_digest(request.query_params.get("key", ""), API_KEY):
        return True
    return False


@app.get("/")
async def root():
    return {"name": "ePepper", "status": "ok"}


@app.get("/version")
async def version(request: Request):
    """Returns current image hash + metadata. ESP32 polls this to check for changes."""
    if not _check_api_key(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    state = display_state.get()
    return {
        "hash": state["hash"],
        "page": state["page"],
        "total_pages": state["total_pages"],
        "updated_at": state["updated_at"],
        "type": state["type"],
    }


@app.get("/image")
async def image(request: Request, page: int = Query(None, ge=1)):
    """Returns the current display image as BMP.

    If no page param is given, serves the current active page from state.
    """
    if not _check_api_key(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    if page is None:
        page = display_state.get()["page"]

    bmp_data = display_state.get_image_bmp(page=page)
    if bmp_data is None:
        return Response(status_code=204)  # no content yet

    state = display_state.get()
    return Response(
        content=bmp_data,
        media_type="image/bmp",
        headers={
            "Cache-Control": "no-store",
            "X-Hash": state["hash"],
            "X-Page": str(state["page"]),
            "X-Total-Pages": str(state["total_pages"]),
        },
    )


@app.post("/page/next")
async def page_next(request: Request):
    """Advance to next page. Called by ESP32 on button press."""
    if not _check_api_key(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    state = display_state.get()
    current = state["page"]
    total = state["total_pages"]

    if total <= 1:
        return {"ok": False, "reason": "single_page", "page": current, "total_pages": total}

    # Wrap around: last page → back to page 1
    new_page = current + 1 if current < total else 1
    display_state.set_page(new_page)
    log.info("Page next: %d → %d (of %d)", current, new_page, total)

    return {"ok": True, "page": new_page, "total_pages": total}


@app.post("/page/prev")
async def page_prev(request: Request):
    """Go to previous page. Called by ESP32 on button press."""
    if not _check_api_key(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    state = display_state.get()
    current = state["page"]
    total = state["total_pages"]

    if total <= 1:
        return {"ok": False, "reason": "single_page", "page": current, "total_pages": total}

    # Wrap around: page 1 → last page
    new_page = current - 1 if current > 1 else total
    display_state.set_page(new_page)
    log.info("Page prev: %d → %d (of %d)", current, new_page, total)

    return {"ok": True, "page": new_page, "total_pages": total}


@app.post("/device/status")
async def device_status(
    request: Request,
    battery_mv: int = Query(0),
    rssi: int = Query(0),
    uptime_s: int = Query(0),
):
    """ESP32 reports its status here."""
    if not _check_api_key(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    display_state.update_device_status(
        battery_mv=battery_mv,
        rssi=rssi,
        uptime_s=uptime_s,
    )
    return {"ok": True}


@app.get("/device/status")
async def get_device_status(request: Request):
    """Returns last known device status (for Telegram /status command)."""
    if not _check_api_key(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    return display_state.get_device_status()
