"""FastAPI server — serves images to the ESP32 display."""

import asyncio
import logging
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import display_state
import library
from api.web import router as web_router
from config import API_KEY, DEVICE_WAKE_HOUR_LOCAL, TZ
from scheduler import seconds_until_next_local_hour

log = logging.getLogger(__name__)
app = FastAPI(title="ePepper", version="0.1.0")

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app.mount("/app/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")
app.include_router(web_router)


def _check_api_key(request: Request) -> bool:
    """Validate auth from Authorization header or a /app/ session cookie.

    The query-param fallback was dropped — uvicorn's access log records the
    full path+query, so passing the key in `?key=` leaked it on every request.
    """
    # Device path: Bearer token == raw API key.
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and secrets.compare_digest(auth[7:], API_KEY):
        return True
    # Browser path: random session token minted by /app/login.
    if library.validate_session(request.cookies.get("epepper_auth", "")):
        return True
    return False


@app.get("/")
async def root():
    return {"name": "ePepper", "status": "ok"}


@app.get("/version")
async def version(request: Request):
    """Returns current image hash + metadata. ESP32 hits this on every wake
    (button press or daily timer) to decide whether to refetch /image."""
    if not _check_api_key(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    state = display_state.get()
    # Tell the firmware how many seconds to sleep so its next timer wake
    # lands at DEVICE_WAKE_HOUR_LOCAL local time. Recomputed on every
    # request so the value is always fresh; the device just trusts it
    # and avoids needing to know UTC ↔ local conversion or DST.
    next_wake = int(seconds_until_next_local_hour(
        datetime.now(TZ), DEVICE_WAKE_HOUR_LOCAL,
    ))
    return {
        "hash": state["hash"],
        "page": state["page"],
        "total_pages": state["total_pages"],
        "updated_at": state["updated_at"],
        "type": state["type"],
        "lang": state["lang"],
        "next_wake_in_s": next_wake,
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


@app.post("/page/first")
async def page_first(request: Request):
    """Jump to page 1. Called by ESP32 on long-press of the prev button."""
    if not _check_api_key(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    state = display_state.get()
    total = state["total_pages"]

    if total <= 1:
        return {"ok": False, "reason": "single_page", "page": state["page"], "total_pages": total}

    display_state.set_page(1)
    log.info("Page first: %d → 1 (of %d)", state["page"], total)
    return {"ok": True, "page": 1, "total_pages": total}


@app.post("/display/clear")
async def display_clear(request: Request):
    """Clear the panel. Fires on the device's PREV + REFRESH chord."""
    if not _check_api_key(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    display_state.clear()
    log.info("Display cleared via /display/clear")
    return {"ok": True}


@app.post("/page/last")
async def page_last(request: Request):
    """Jump to the last page. Called by ESP32 on long-press of the next button."""
    if not _check_api_key(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    state = display_state.get()
    total = state["total_pages"]

    if total <= 1:
        return {"ok": False, "reason": "single_page", "page": state["page"], "total_pages": total}

    display_state.set_page(total)
    log.info("Page last: %d → %d (of %d)", state["page"], total, total)
    return {"ok": True, "page": total, "total_pages": total}


# Strong refs to in-flight fire-and-forget tasks. asyncio only tracks them
# weakly, so without this the GC can eat a task mid-execution.
_background_tasks: set[asyncio.Task] = set()


def _log_task_exception(task: asyncio.Task) -> None:
    """Done-callback that logs any unhandled exception from a fire-and-forget task."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.exception("Background task %r raised:", task.get_name(), exc_info=exc)


@app.post("/device/status")
async def device_status(
    request: Request,
    battery_mv: int = Query(0),
    rssi: int = Query(0),
    temperature_c: float | None = Query(None),
    humidity_pct: float | None = Query(None),
):
    """ESP32 wake-cycle report — fires on a button press and on the daily
    timer wake. `temperature_c` / `humidity_pct` are optional so a
    pre-SHT40 firmware build still posts a valid request.
    """
    if not _check_api_key(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    result = display_state.update_device_status(
        battery_mv=battery_mv,
        rssi=rssi,
        temperature_c=temperature_c,
        humidity_pct=humidity_pct,
    )

    alert_mv = result.get("low_battery_alert_mv")
    if alert_mv is not None:
        # Dispatch off the request path — we don't want to block the device's
        # POST on Telegram delivery, and a Telegram send timeout shouldn't
        # 5xx the device. Imported here to keep the api module free of bot deps.
        from bot.handlers import notify_low_battery
        task = asyncio.create_task(
            notify_low_battery(alert_mv), name="notify_low_battery"
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        task.add_done_callback(_log_task_exception)

    return {"ok": True}


@app.get("/device/status")
async def get_device_status(request: Request):
    """Returns last known device status (for Telegram /status command)."""
    if not _check_api_key(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    return display_state.get_device_status()
