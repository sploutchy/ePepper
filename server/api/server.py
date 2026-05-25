"""FastAPI server — serves images to the ESP32 display.

Auth (SEC-NEW-2): _check_api_key accepts the browser session cookie as an
alternative to the Bearer token ONLY when a route opts in via
allow_cookie=True. Just /image does so, because the status page renders the
live display preview with `<img src="/image?v=...">` (see
web/templates/_status_body.html:56): that <img> tag carries the session
cookie but cannot easily attach a Bearer header. Every other device endpoint
— notably /firmware/download, whose .bin carries the baked WiFi password +
API key — is Bearer-only, so a leaked status-page session can't be used to
exfiltrate firmware credentials.
"""

import asyncio
import logging
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import device_telemetry
from display import state as display_state
import library
from api.web import cookie_is_valid, router as web_router
from display.image import get_image_bmp
from config import API_KEY, DEVICE_WAKE_HOUR_LOCAL, TZ
from scheduler import seconds_until_next_local_hour

log = logging.getLogger(__name__)
app = FastAPI(title="ePepper", version="0.1.0")

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app.mount("/app/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")
app.include_router(web_router)


def _check_api_key(request: Request, allow_cookie: bool = False) -> bool:
    """Validate auth from the Authorization header, optionally also from a
    /app/ session cookie.

    The Bearer token (== raw API key) is always accepted. The browser
    session cookie is accepted ONLY when `allow_cookie=True` (SEC-NEW-2):
    just /image opts in, for the status-page `<img src="/image">` preview
    that can't attach a Bearer header. Every other device endpoint stays
    Bearer-only so a status-page session can't reach e.g.
    /firmware/download and pull the baked credentials out of the .bin.

    The query-param fallback was dropped — uvicorn's access log records the
    full path+query, so passing the key in `?key=` leaked it on every request.
    """
    # Device path: Bearer token == raw API key. Compare as bytes — passing
    # raw str to compare_digest raises TypeError on non-ASCII input, which
    # would surface as a 500 instead of an auth failure for a junk header.
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and secrets.compare_digest(
        auth[7:].encode("utf-8"), API_KEY.encode("utf-8"),
    ):
        return True
    # Browser path: the /app/ auth cookie (an HMAC of the API key, minted by
    # /app/login). Only honored on cookie-allowed routes (/image).
    if allow_cookie and cookie_is_valid(request.cookies.get("epepper_auth", "")):
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
        # Stable across page navigation; flips only on a new render. The
        # device keys its on-flash page cache on this so page turns can be
        # served offline and a new recipe still invalidates the cache.
        "content_hash": state["content_hash"],
        # No `page` field (DES-D): the device computes its own page locally.
        # The field existed only to drive web-preview clicks and was removed.
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
    # allow_cookie=True: the status page previews this via
    # `<img src="/image">`, which carries the session cookie but no Bearer
    # header (SEC-NEW-2). This is the only device endpoint that accepts the
    # cookie.
    if not _check_api_key(request, allow_cookie=True):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    if page is None:
        page = display_state.get()["page"]

    bmp_data = get_image_bmp(page=page)
    if bmp_data is None:
        return Response(status_code=204)  # no content yet

    if _is_device_fetch(request):
        pending = display_state.consume_pending_displayed_bump()
        if pending is not None:
            library.touch_displayed(pending)

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


_DEVICE_UA_PREFIX = "ePepper-device/"


def _is_device_fetch(request: Request) -> bool:
    """Distinguish ESP32 /image fetches from browser status-page previews.

    The firmware sets a `User-Agent` starting with `ePepper-device/`;
    browser status-page previews of /image don't carry it, so they don't
    count as a cook.
    """
    return request.headers.get("user-agent", "").startswith(_DEVICE_UA_PREFIX)


@app.post("/display/clear")
async def display_clear(request: Request):
    """Clear the panel. Triggered by the web status page's Clear button
    and the bot's /clear command."""
    if not _check_api_key(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    display_state.clear()
    log.info("Display cleared via /display/clear")
    return {"ok": True}


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
    firmware_version: int | None = Query(None),
):
    """ESP32 wake-cycle report — fires on a button press and on the daily
    timer wake. `temperature_c` / `humidity_pct` / `firmware_version` are
    optional so older firmware builds that don't yet report them still post
    valid requests.
    """
    if not _check_api_key(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    result = device_telemetry.update_device_status(
        battery_mv=battery_mv,
        rssi=rssi,
        temperature_c=temperature_c,
        humidity_pct=humidity_pct,
        firmware_version=firmware_version,
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

    return device_telemetry.get_device_status()


# ---- OTA firmware updates ----
# Bind-mounted from the host via docker-compose; populated by the Firmware
# CI workflow (firmware.bin + version.txt). The device hits /firmware/version
# on every daily wake; if the integer in version.txt > the FIRMWARE_VERSION
# baked into the running build, it pulls /firmware/download and self-flashes
# via Update.h. Both routes are Bearer-authed since the .bin contains the
# baked WiFi password + API key.

_FIRMWARE_DIR = Path("/app/firmware")


@app.get("/firmware/version")
async def firmware_version(request: Request):
    if not _check_api_key(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    version_file = _FIRMWARE_DIR / "version.txt"
    if not version_file.exists():
        # No firmware published yet — report 0 so any running device stays put.
        return PlainTextResponse("0")
    return PlainTextResponse(version_file.read_text().strip())


@app.get("/firmware/download")
async def firmware_download(request: Request):
    if not _check_api_key(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    bin_file = _FIRMWARE_DIR / "firmware.bin"
    if not bin_file.exists():
        return JSONResponse(status_code=404, content={"error": "firmware not published"})
    return FileResponse(bin_file, media_type="application/octet-stream")
