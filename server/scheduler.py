"""Background schedulers.

`midnight_loop` is the daily multipurpose tick. At every local midnight
it runs the day's chores in order:

  1. Push a recipe to the display — a saved recipe whose calendar day
     matches today (any past year), falling back to one of Fooby's
     "Inspiration de la semaine" recipes (French), rotated by ISO
     weekday so the seven slots cycle deterministically through the
     week.
  2. Trigger a daily DB backup, which uploads a gzipped snapshot to
     the configured Telegram chat only if the library was written to
     since the previous upload.

Manual Telegram pushes during the day are preserved — they win until
the next midnight tick.

`heartbeat_loop` wakes hourly to alert on the device falling silent.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import backup
import display_state
import library
from config import TZ
from display_push import push_recipe_to_display
from processing.fooby_inspiration import fetch_weekly_inspiration_urls
from processing.recipes import process_recipe_url

log = logging.getLogger(__name__)


def _seconds_until_next_local_midnight(now: datetime) -> float:
    """Real-time seconds until the next 00:00 in the configured TZ.

    `now` must be timezone-aware. The next midnight is built by advancing
    the calendar date (so DST never produces a non-midnight result), then
    both sides are normalised to UTC before subtracting — aware-datetime
    subtraction in CPython preserves the wall-clock interval when the
    offsets differ, which would hide the DST hour we're trying to honour.
    """
    tomorrow = now.date() + timedelta(days=1)
    next_midnight = datetime.combine(tomorrow, datetime.min.time(), tzinfo=now.tzinfo)
    return (next_midnight.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()


def _push_anniversary_for(today: datetime) -> bool:
    """Push today's anniversary recipe if one exists. Returns True when handled."""
    row = library.pick_anniversary_recipe(today.strftime("%m-%d"), today.year)
    if row is None:
        return False
    # Skip the push if this recipe is already the active display content —
    # otherwise the device wakes and does a full panel refresh for no
    # visible change (e.g. user pushed today's anniversary manually
    # earlier in the day).
    state = display_state.get()
    if state.get("type") == "recipe" and state.get("recipe_id") == row["id"]:
        log.info(
            "Anniversary recipe id=%d already on display; skipping push",
            row["id"],
        )
        return True
    push_recipe_to_display(row)
    log.info(
        "Pushed anniversary recipe id=%d title=%r (originally saved %s)",
        row["id"], row["title"],
        datetime.fromtimestamp(row["saved_at"]).date().isoformat(),
    )
    return True


async def _push_fooby_inspiration_for(today: datetime) -> None:
    """Push one Fooby weekly-inspiration recipe, indexed by today's weekday.

    Transient: not added to the library. The user can still save it later
    by sending the URL to the Telegram bot. No retry — if the picked URL
    fails to parse, the display is left unchanged.
    """
    try:
        urls = await fetch_weekly_inspiration_urls()
    except Exception:
        log.exception("Fooby inspiration: fetch failed; leaving display unchanged")
        return
    if not urls:
        log.info("Fooby inspiration: no recipe URLs found; leaving display unchanged")
        return

    # Mon=0..Sun=6 → deterministic rotation, same slot every week.
    # Modulo keeps it safe when fewer than seven URLs are published.
    idx = today.weekday() % len(urls)
    url = urls[idx]

    state = display_state.get()
    if state.get("type") == "recipe" and state.get("url") == url:
        log.info("Fooby inspiration %s already on display; skipping push", url)
        return

    recipe = await process_recipe_url(url)
    if recipe is None:
        log.info("Fooby inspiration: failed to parse %s; leaving display unchanged", url)
        return

    display_state.set_recipe(
        recipe,
        comments=[],
        rating=None,
        recipe_id=None,
        url=url,
    )
    log.info(
        "Pushed Fooby weekly inspiration (weekday=%d, slot=%d/%d): %r (%s)",
        today.weekday(), idx, len(urls), recipe.get("title"), url,
    )


async def midnight_loop() -> None:
    """Run forever: sleep until next local midnight, run each day's chores
    in order (anniversary push, then DB backup), survive failures in any
    one so the others still execute. Each chore handles its own no-op
    case (e.g. backup skips the upload when nothing changed)."""
    while True:
        now = datetime.now(TZ)
        sleep_s = _seconds_until_next_local_midnight(now)
        log.info("Midnight scheduler sleeping %.0fs until next local midnight", sleep_s)
        try:
            await asyncio.sleep(sleep_s)
        except asyncio.CancelledError:
            log.info("Midnight scheduler cancelled")
            raise
        try:
            today = datetime.now(TZ)
            if not _push_anniversary_for(today):
                log.info(
                    "No anniversary recipe for %s; falling back to Fooby weekly inspiration",
                    today.date().isoformat(),
                )
                await _push_fooby_inspiration_for(today)
        except Exception:
            log.exception("Midnight push failed; backup will still run")
        try:
            await backup.flush_if_dirty()
        except Exception:
            log.exception("Daily backup failed; will retry tomorrow")


_HEARTBEAT_CHECK_INTERVAL_S = 3600  # hourly is fine — alert fires once per stale episode


async def heartbeat_loop() -> None:
    """Wake hourly to check whether the device's heartbeat went stale.

    Proactive (not reactive) because the absence of POSTs is the signal —
    can't piggyback on update_device_status the way the battery alert does.
    """
    while True:
        try:
            await asyncio.sleep(_HEARTBEAT_CHECK_INTERVAL_S)
        except asyncio.CancelledError:
            log.info("Heartbeat scheduler cancelled")
            raise
        try:
            hours_since = display_state.check_heartbeat_stale()
            if hours_since is not None:
                # Lazy import mirrors api/server.py's notify_low_battery wiring
                # and avoids a circular import at module load time.
                from bot.handlers import notify_stale_heartbeat
                await notify_stale_heartbeat(hours_since)
        except Exception:
            log.exception("Heartbeat check failed; will retry next hour")
