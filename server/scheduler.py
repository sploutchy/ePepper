"""Background schedulers.

Currently only `anniversary_loop`, which at every local midnight selects a
saved recipe whose calendar day matches today (from any past year) and
pushes it to the display. Manual Telegram pushes during the day are
preserved — they win until the next midnight tick.

Per the locked plan: library-only, no external fallback. If no
anniversary candidate exists, the display is left unchanged.
"""

import asyncio
import logging
from datetime import datetime, timedelta

import display_state
import library
from bot.handlers import push_recipe_to_display

log = logging.getLogger(__name__)


def _seconds_until_next_local_midnight(now: datetime) -> float:
    tomorrow = (now + timedelta(days=1)).date()
    next_midnight = datetime.combine(tomorrow, datetime.min.time())
    return (next_midnight - now).total_seconds()


def _push_anniversary_for(today: datetime) -> None:
    row = library.pick_anniversary_recipe(today.strftime("%m-%d"), today.year)
    if row is None:
        log.info("No anniversary recipe for %s; leaving display unchanged", today.date().isoformat())
        return
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
        return
    push_recipe_to_display(row)
    log.info(
        "Pushed anniversary recipe id=%d title=%r (originally saved %s)",
        row["id"], row["title"],
        datetime.fromtimestamp(row["saved_at"]).date().isoformat(),
    )


async def anniversary_loop() -> None:
    """Run forever: sleep until next local midnight, then push an anniversary."""
    while True:
        now = datetime.now()
        sleep_s = _seconds_until_next_local_midnight(now)
        log.info("Anniversary scheduler sleeping %.0fs until next local midnight", sleep_s)
        try:
            await asyncio.sleep(sleep_s)
        except asyncio.CancelledError:
            log.info("Anniversary scheduler cancelled")
            raise
        try:
            _push_anniversary_for(datetime.now())
        except Exception:
            log.exception("Anniversary push failed; will retry tomorrow")


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
