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
from datetime import date, datetime, time, timedelta, timezone

import backup
import device_telemetry
from display import state as display_state
from processing import fooby_cache
import library
from config import TZ
from display.push import push_recipe_to_display
from processing.fooby_inspiration import fetch_weekly_inspiration_urls
from processing.recipes import IngestError, ingest_recipe, process_recipe_url

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


def seconds_until_next_local_hour(now: datetime, target_hour: int) -> float:
    """Real-time seconds until the next HH:00 wall-clock time in `now.tzinfo`.

    Used by `/version` so the e-ink firmware can align its daily timer
    wake to a fixed local hour instead of drifting by 24 h from the
    last button press. DST-aware: target time is built as a wall-clock
    value, then both sides are normalised to UTC before subtracting.
    Spring-forward nights that skip the target hour return ~23 h until
    the next one; fall-back duplicates resolve to the earlier of the
    two.

    A sub-minute result (a button press in the last seconds before the
    target hour) is rolled forward to the FOLLOWING day's target. The
    firmware's computeSleepSeconds treats next_wake_in_s < MIN_SLEEP_S
    (60 s) as clock skew and falls back to a flat 24 h sleep, which would
    drift the daily wake; returning the next day's ~24 h figure instead
    keeps the device aligned to the local hour.
    """
    target_t = time(target_hour, 0)
    today_target = datetime.combine(now.date(), target_t, tzinfo=now.tzinfo)
    if today_target > now:
        target = today_target
    else:
        target = datetime.combine(
            now.date() + timedelta(days=1), target_t, tzinfo=now.tzinfo,
        )
    seconds = (target.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()
    if seconds < 60:
        target = datetime.combine(
            target.date() + timedelta(days=1), target_t, tzinfo=now.tzinfo,
        )
        seconds = (target.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()
    return seconds


def _push_anniversary_for(today: datetime) -> bool:
    """Push today's anniversary recipe if one exists. Returns True when handled
    (either pushed successfully or already on display); False when no candidate
    exists or rendering failed — in which case the caller should run the Fooby
    fallback so the panel doesn't stay frozen on yesterday's content.

    The "skip if already on display" optimization now lives in
    push_recipe_to_display itself, so the True branch covers both
    "rendered fresh" and "already showing this row" (the latter logs
    inside push_recipe_to_display).
    """
    row = library.pick_anniversary_recipe(today.strftime("%m-%d"), today.year)
    if row is None:
        return False
    if not push_recipe_to_display(row):
        log.warning(
            "Anniversary push failed for id=%d; caller should fall back",
            row["id"],
        )
        return False
    log.info(
        "Pushed anniversary recipe id=%d title=%r (last cooked %s)",
        row["id"], row["title"],
        datetime.fromtimestamp(row["last_displayed_at"]).date().isoformat(),
    )
    return True


async def _push_fooby_inspiration_for(today: datetime) -> None:
    """Push one Fooby weekly-inspiration recipe, indexed by today's weekday.

    Prefers the pre-fetched cache (`fooby_cache`, populated by the previous
    tick's `_prefetch_fooby_for`) so the recipe the status page advertised
    as "Tomorrow" is exactly what lands on the panel. Falls back to a live
    fetch on first deploy, after a cache miss, or when yesterday's prefetch
    failed.

    Transient: not added to the library. The user can still save it later
    by sending the URL to the Telegram bot. No retry — if the picked URL
    fails to parse, the display is left unchanged.
    """
    today_iso = today.date().isoformat()
    url: str | None = None
    cached = fooby_cache.get()
    if cached and cached.get("for_date") == today_iso:
        url = cached.get("url")
        log.info("Fooby inspiration: using pre-fetched URL %s", url)

    if url is None:
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

    # ingest_recipe handles the "already on display" short-circuit
    # internally (compares URL when the recipe isn't in the library);
    # action == "already-active" just logs and bails. A parse miss raises
    # IngestError, which we swallow so the panel stays on whatever it was
    # showing rather than going blank.
    try:
        result = await ingest_recipe(url, push=True, persist=False)
    except IngestError:
        log.info("Fooby inspiration: failed to parse %s; leaving display unchanged", url)
        return

    if result["action"] == "already-active":
        log.info("Fooby inspiration %s already on display; skipping push", url)
        return
    if result["action"] == "parsed-only":
        log.warning(
            "Fooby inspiration: parse ok but push failed for %s", url,
        )
        return
    log.info(
        "Pushed Fooby weekly inspiration: %r (%s)",
        result["recipe"].get("title"), url,
    )


async def _prefetch_fooby_for(target: date) -> None:
    """Resolve + cache the Fooby pick that would play on `target`.

    Skipped when `target` already has an anniversary candidate — in that
    case the midnight scheduler would push the anniversary, not a Fooby
    recipe, so caching one would be misleading on the status preview.

    Best-effort: every failure is logged, never raised. A miss just leaves
    the cache stale; the status page falls back to a generic hint, and the
    next midnight tick re-fetches live.
    """
    anniv = library.pick_anniversary_recipe(
        target.strftime("%m-%d"), target.year
    )
    if anniv is not None:
        log.info(
            "Fooby prefetch: anniversary covers %s (id=%d); skipping",
            target.isoformat(), anniv["id"],
        )
        return
    try:
        urls = await fetch_weekly_inspiration_urls()
    except Exception:
        log.exception("Fooby prefetch: fetch failed for %s", target.isoformat())
        return
    if not urls:
        log.info("Fooby prefetch: no recipe URLs found for %s", target.isoformat())
        return
    idx = target.weekday() % len(urls)
    url = urls[idx]
    recipe = await process_recipe_url(url)
    if recipe is None:
        log.info("Fooby prefetch: failed to parse %s", url)
        return
    title = recipe.get("title") or url
    fooby_cache.set_pick(target, url, title)


async def backfill_translations() -> None:
    """One-shot pass to populate `recipes.translated_keywords` for old rows.

    Walks every saved recipe whose `translated_keywords` is NULL, runs it
    through `translate_for_search`, and writes the result back. Bounded
    concurrency (3) keeps the LLM endpoint happy and the local DB locked
    for short bursts only.

    Translation failures are tolerated — the helper logs and skips them;
    the row simply stays NULL and gets retried on the next container
    start. This is fire-and-forget, and never raises.
    """
    from processing.recipes import translate_for_search
    from processing import llm

    if not llm.is_enabled():
        log.info("Translation backfill skipped — LLM not configured")
        return

    try:
        pending = library.recipes_needing_translation()
    except Exception:
        log.exception("Translation backfill: failed to enumerate rows")
        return
    if not pending:
        log.info("Translation backfill: nothing to do")
        return
    log.info("Translation backfill: %d recipes pending", len(pending))

    sem = asyncio.Semaphore(3)

    async def _one(row: dict) -> None:
        async with sem:
            try:
                blob = await translate_for_search({
                    "title": row["title"],
                    "ingredients": row["recipe"].get("ingredients") or [],
                    "lang": row["lang"],
                })
            except Exception:
                log.exception("Translation backfill: LLM call crashed for id=%d", row["id"])
                return
            if not blob:
                # Don't keep retrying a recipe we already failed on. Mark
                # the row with an empty-string sentinel so the next
                # restart's enumeration skips it.
                blob = ""
            try:
                library.set_translated_keywords(row["id"], blob)
            except Exception:
                log.exception(
                    "Translation backfill: DB write failed for id=%d", row["id"]
                )

    try:
        await asyncio.gather(*(_one(row) for row in pending))
    except Exception:
        log.exception("Translation backfill: gather failed")
    log.info("Translation backfill complete")


async def initial_fooby_prefetch() -> None:
    """One-shot prefetch on container start when the cache isn't current.

    Without this, a fresh deploy or a restart between midnights would leave
    the status "Tomorrow" card showing the generic hint until the next
    midnight tick. With it, the preview lands as soon as the server is up.
    """
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).date()
    cached = fooby_cache.get()
    if cached and cached.get("for_date") == tomorrow.isoformat():
        log.info("Initial Fooby prefetch: cache already current for %s", tomorrow.isoformat())
        return
    log.info("Initial Fooby prefetch: refreshing for %s", tomorrow.isoformat())
    try:
        await _prefetch_fooby_for(tomorrow)
    except Exception:
        log.exception("Initial Fooby prefetch failed")


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
            # Pre-fetch tomorrow's Fooby pick so the web "Tomorrow" card has
            # a concrete recipe to show, and so the next tick has a
            # deterministic URL to push (matching what the user saw on the
            # preview). Skipped internally when tomorrow has an anniversary.
            tomorrow = (datetime.now(TZ) + timedelta(days=1)).date()
            await _prefetch_fooby_for(tomorrow)
        except Exception:
            log.exception("Fooby prefetch failed; status page will fall back to generic hint")
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
            hours_since = device_telemetry.check_heartbeat_stale()
            if hours_since is not None:
                # Lazy import mirrors api/server.py's notify_low_battery wiring
                # and avoids a circular import at module load time.
                from bot.handlers import notify_stale_heartbeat
                await notify_stale_heartbeat(hours_since)
        except Exception:
            log.exception("Heartbeat check failed; will retry next hour")
