"""Telegram bot handlers for ePepper."""

import asyncio
import html
import logging
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Tuple

from telegram import BotCommand, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from config import (
    ALLOWED_USERS,
    BACKUP_CHAT_ID,
    LLM_API_KEY,
    LLM_API_URL,
    TELEGRAM_BOT_TOKEN,
    TZ,
    WEB_URL,
)
import backup
import device_telemetry
from display import state as display_state
import library
from display.push import push_recipe_to_display
from processing.recipes import (
    IngestError,
    ingest_recipe,
    translate_for_search,
)
from status_helpers import battery_pct, humanize_ago, humanize_date, rssi_quality, source_name

log = logging.getLogger(__name__)


# Pending unsaved recipes, keyed by a short token embedded in the Save
# button's callback_data. The map only holds parsed recipes the user has
# not yet rated; once a star is tapped (or 32 newer pushes have arrived)
# the entry is removed.
_PENDING_MAX = 32
_pending: "OrderedDict[str, Tuple[str, dict]]" = OrderedDict()

# Notes the user typed *before* saving a pending recipe — populated by
# /comment when the active display content isn't yet in the repertoire.
# Tapping "Save & add note" drains both `_pending` and this map together.
_pending_notes: dict[str, str] = {}

# Telegram's per-message API limit is 4096 chars; we cap user-supplied
# /comment text well below that so the echoed confirmation (which wraps
# the note in extra prose + HTML) can never exceed the limit and raise
# BadRequest mid-handler, which would orphan the _pending_notes entry.
_COMMENT_MAX_CHARS = 3500

# Short tokens → search queries, so paginated /search buttons can carry a
# 6-char ref in their 64-byte callback_data instead of stuffing the full
# query (and risking truncation / encoding issues).
_SEARCH_QUERIES_MAX = 32
_search_queries: "OrderedDict[str, str]" = OrderedDict()
_SEARCH_PAGE_SIZE = 5


# Set by create_bot() so out-of-band code paths (e.g. low-battery alerts
# from the /device/status endpoint) can push messages without needing the
# Application instance threaded through.
_bot_app: Application | None = None


def _alert_recipients() -> list[int]:
    """Chat IDs that should receive out-of-band device alerts.

    Prefers `ALLOWED_USERS`. Falls back to `BACKUP_CHAT_ID` so a freshly
    deployed bot with empty ALLOWED_USERS still reaches the operator
    through the backup channel they already trust. Returns [] only when
    neither is configured.
    """
    if ALLOWED_USERS:
        return list(ALLOWED_USERS)
    if BACKUP_CHAT_ID is not None:
        return [BACKUP_CHAT_ID]
    return []


async def notify_low_battery(battery_mv: int) -> None:
    """Push a one-shot low-battery warning to every allowed user.

    Called by the FastAPI /device/status handler the first time a wake-cycle
    report comes in below the threshold (device_telemetry owns the hysteresis).
    Falls back to BACKUP_CHAT_ID when ALLOWED_USERS is empty so the alert
    still reaches the operator before the bot is fully configured.
    """
    if _bot_app is None:
        log.warning("notify_low_battery: bot not yet initialised")
        return
    recipients = _alert_recipients()
    if not recipients:
        log.warning(
            "notify_low_battery: neither ALLOWED_USERS nor BACKUP_CHAT_ID configured, skipping alert"
        )
        return
    text = (
        f"🪫 ePepper battery is low: {battery_pct(battery_mv)}% "
        f"({battery_mv / 1000:.2f} V) — charge soon."
    )
    for uid in recipients:
        try:
            await _bot_app.bot.send_message(chat_id=uid, text=text)
            log.info("Low-battery alert sent to chat %s (%dmV)", uid, battery_mv)
        except Exception:
            log.exception("Failed to send low-battery alert to chat %s", uid)


async def notify_stale_heartbeat(hours_since: int) -> None:
    """Push a one-shot warning when the device hasn't checked in for ≥25 h.

    Called by the scheduler's heartbeat_loop the first time the staleness
    threshold is crossed (device_telemetry owns the alerted flag). Re-armed
    automatically on the next successful /device/status POST. Falls back
    to BACKUP_CHAT_ID when ALLOWED_USERS is empty.
    """
    if _bot_app is None:
        log.warning("notify_stale_heartbeat: bot not yet initialised")
        return
    recipients = _alert_recipients()
    if not recipients:
        log.warning(
            "notify_stale_heartbeat: neither ALLOWED_USERS nor BACKUP_CHAT_ID configured, skipping alert"
        )
        return
    text = (
        f"⚠️ ePepper hasn't checked in for {hours_since}h — "
        f"battery may be flat or Wi-Fi down."
    )
    for uid in recipients:
        try:
            await _bot_app.bot.send_message(chat_id=uid, text=text)
            log.info("Stale-heartbeat alert sent to chat %s (%dh)", uid, hours_since)
        except Exception:
            log.exception("Failed to send stale-heartbeat alert to chat %s", uid)


def _stash_pending(url: str, recipe: dict) -> str:
    token = uuid.uuid4().hex[:8]
    _pending[token] = (url, recipe)
    while len(_pending) > _PENDING_MAX:
        evicted, _ = _pending.popitem(last=False)
        # Drop any orphaned note for the evicted token so the dict can't
        # leak unbounded notes for tokens that no longer exist.
        _pending_notes.pop(evicted, None)
    return token


def _find_pending_token_for_url(url: str | None) -> str | None:
    """Return the most recent pending token whose URL matches `url`, or None.

    Used by /comment to chain "save the active push + add this note" — the
    active display state knows the URL but not which pending token it came
    from, so we scan back-to-front (insertion order = oldest first).
    """
    if not url:
        return None
    for tok in reversed(_pending):
        u, _ = _pending[tok]
        if u == url:
            return tok
    return None


def _stash_search(query: str) -> str:
    token = uuid.uuid4().hex[:6]
    _search_queries[token] = query
    while len(_search_queries) > _SEARCH_QUERIES_MAX:
        _search_queries.popitem(last=False)
    return token


# Commands surfaced in Telegram's native blue `/` menu. Order is the order
# the menu shows them, so daily-use commands come first. /start is omitted
# — it's a bootstrapping command.
_BOT_COMMANDS: list[tuple[str, str]] = [
    ("recipe", "Push a recipe URL to the display"),
    ("search", "Find a saved recipe"),
    ("surprise", "Pick a random saved recipe"),
    ("comment", "Add a note to the displayed recipe"),
    ("status", "Device + repertoire status"),
    ("clear", "Clear the display"),
    ("help", "Show all commands"),
]


async def _register_commands(app: Application) -> None:
    """Populate Telegram's blue `/` menu so commands are discoverable.

    Runs as the Application's post_init hook so set_my_commands() lands
    once on startup. Failure is non-fatal — the bot still works without
    the menu, which is just a nicety.
    """
    try:
        await app.bot.set_my_commands(
            [BotCommand(name, desc) for name, desc in _BOT_COMMANDS]
        )
        log.info("Registered %d bot commands with Telegram", len(_BOT_COMMANDS))
    except Exception:
        log.exception("Failed to register bot commands; menu will be empty")


def create_bot() -> Application:
    """Create and configure the Telegram bot application."""
    global _bot_app
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(_register_commands)
        .build()
    )
    _bot_app = app
    backup.set_bot(app.bot)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("recipe", cmd_recipe))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("comment", cmd_comment))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("surprise", cmd_surprise))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_save_button, pattern=r"^save:"))
    app.add_handler(CallbackQueryHandler(on_save_note_button, pattern=r"^save_note:"))
    app.add_handler(CallbackQueryHandler(on_note_cancel, pattern=r"^note:cancel:"))
    app.add_handler(CallbackQueryHandler(on_push_button, pattern=r"^push:"))
    app.add_handler(CallbackQueryHandler(on_search_preview, pattern=r"^search_preview:"))
    app.add_handler(CallbackQueryHandler(on_search_back, pattern=r"^search_back:"))
    app.add_handler(CallbackQueryHandler(on_search_nav, pattern=r"^search:"))
    app.add_handler(CallbackQueryHandler(on_surprise_again, pattern=r"^surprise_again:"))
    app.add_handler(CallbackQueryHandler(on_surprise_push, pattern=r"^surprise_push:"))
    app.add_handler(CallbackQueryHandler(on_clear_button, pattern=r"^clear:"))
    app.add_handler(CallbackQueryHandler(on_quick_action, pattern=r"^quick:"))
    # Catch-all for unknown /commands — must register after every named
    # CommandHandler so known commands match first.
    app.add_handler(MessageHandler(filters.COMMAND, on_unknown_command))

    return app


async def on_unknown_command(update: Update, context) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    await update.message.reply_text("Unknown command — try /help.")


# One-shot guard so the empty-ALLOWED_USERS warning prints once per process
# (first time anyone is checked) instead of on every incoming message.
_empty_allowed_users_warned: bool = False


def _is_allowed(user_id: int) -> bool:
    """Check if user is allowed (empty list = deny all)."""
    global _empty_allowed_users_warned
    if not ALLOWED_USERS:
        if not _empty_allowed_users_warned:
            log.warning(
                "ALLOWED_USERS env var is empty — bot will reject every user. "
                "Set ALLOWED_USERS to a comma-separated list of Telegram user IDs."
            )
            _empty_allowed_users_warned = True
        return False
    return user_id in ALLOWED_USERS


def _web_app_line() -> str:
    """One-line pointer to the web app, formatted for HTML mode.

    Linkified when WEB_URL is set in the environment; otherwise just
    describes the path so the user can navigate manually.
    """
    if WEB_URL:
        return (
            f"🌐 <b>Web app:</b> "
            f"<a href=\"{html.escape(WEB_URL)}/app/\">{html.escape(WEB_URL)}/app/</a> "
            "(same API_KEY logs you in) — sort, filter, and browse the full repertoire."
        )
    return (
        "🌐 <b>Web app:</b> open <code>/app/</code> on your server "
        "(same API_KEY logs you in) — sort, filter, and browse the full repertoire."
    )


_START_TEXT = (
    "🫑 <b>ePepper — your kitchen recipe display</b>\n\n"
    "<b>Send me:</b>\n"
    "• A photo of a recipe — OCR'd into your repertoire automatically\n"
    "• A recipe URL (just paste the link) — falls back to an LLM if the "
    "site isn't a known one\n\n"
    "<i>Note: pasted URLs are pushed to the panel immediately. Use the "
    "web app to save without displaying.</i>\n\n"
    "Tap 💾 <b>Save</b> under a pushed recipe to keep it in your repertoire. "
    "Use the device's <b>physical buttons</b> to cycle between recipe "
    "pages.\n\n"
    "{web_line}\n\n"
    "Type /help for the full command list."
)


async def cmd_start(update: Update, context) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        _START_TEXT.format(web_line=_web_app_line()),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# Sectioned help — cleaner skim than one long wall of slash commands.
# Each section starts with a one-line "what this is for" then lists the
# commands that belong to it. The native /-menu (set via set_my_commands)
# covers the most-used ones, so /help is the deep reference.
_HELP_TEXT = (
    "🫑 <b>ePepper — help</b>\n\n"
    "<b>➕ Add a recipe</b>\n"
    "Just paste a URL or send a photo of a cookbook / magazine page.\n"
    "<i>Pasted URLs are pushed to the panel immediately — use the web app "
    "to save without displaying.</i>\n"
    "  /recipe &lt;url&gt; — force-parse a URL\n\n"
    "<b>📚 Repertoire</b>\n"
    "Tap 💾 Save under a push to keep a recipe.\n"
    "  /search &lt;query&gt; — find a saved recipe (paginated)\n"
    "  /surprise — pick a random saved recipe\n"
    "  /comment &lt;text&gt; — add a note to what's on screen\n\n"
    "<b>📺 Display</b>\n"
    "Physical buttons cycle pages.\n"
    "  /clear — clear the panel\n\n"
    "<b>ℹ️ Info</b>\n"
    "  /status — device + repertoire snapshot\n"
)


def _help_keyboard() -> InlineKeyboardMarkup | None:
    """Quick-action row under /help — web link, status, surprise.

    Returns None when the only useful button (web) would be missing AND
    we'd be left with a row that just duplicates slash commands. Status +
    Surprise stay regardless because the callbacks are quicker than typing
    the command on mobile.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if WEB_URL:
        rows.append([InlineKeyboardButton("🌐 Open web app", url=f"{WEB_URL}/app/")])
    rows.append([
        InlineKeyboardButton("📊 Status", callback_data="quick:status"),
        InlineKeyboardButton("🎲 Surprise", callback_data="quick:surprise"),
    ])
    return InlineKeyboardMarkup(rows)


async def cmd_help(update: Update, context) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        _HELP_TEXT,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=_help_keyboard(),
    )


async def cmd_recipe(update: Update, context) -> None:
    """Parse a recipe URL and display it."""
    if not _is_allowed(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/recipe &lt;url&gt;</code> — force-parses a URL even "
            "when normal auto-detection misreads it. For most sites you can "
            "just paste the URL directly.",
            parse_mode="HTML",
        )
        return

    url = context.args[0]
    log.info("Recipe command from user %s: %s", update.effective_user.id, url)
    msg = await update.message.reply_text("🔍 Fetching recipe...")
    await _fetch_and_display_recipe(url, msg)


async def cmd_clear(update: Update, context) -> None:
    """Ask for confirmation before wiping the panel.

    Clearing is destructive and there's no undo, so we surface a two-button
    keyboard rather than firing immediately on the unqualified `/clear`.
    """
    if not _is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "🧹 Clear the display?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm clear", callback_data="clear:confirm"),
            InlineKeyboardButton("✖ Cancel", callback_data="clear:cancel"),
        ]]),
    )


async def on_clear_button(update: Update, context) -> None:
    """Resolve the [Confirm] / [Cancel] keyboard surfaced by /clear."""
    query = update.callback_query
    if not _is_allowed(update.effective_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return
    try:
        _, action = query.data.split(":", 1)
    except ValueError:
        await query.answer("Bad callback.", show_alert=True)
        return
    if action == "confirm":
        display_state.clear()
        await query.answer("Cleared")
        try:
            await query.edit_message_text("🧹 Display cleared.")
        except Exception:
            log.exception("on_clear_button: failed to edit message")
    else:
        await query.answer()
        try:
            await query.edit_message_text("Cancelled.")
        except Exception:
            log.exception("on_clear_button: failed to edit message")


def _build_status_text() -> str:
    """Render the /status sectioned snapshot as a single HTML string.

    Extracted from cmd_status so on_quick_action can call it from the
    /help quick-action button without smuggling Update / context through.
    """
    state = display_state.get()
    device = device_telemetry.get_device_status()

    sections = ["🫑 <b>ePepper Status</b>"]

    # Display section — "<b>title</b> from <source> — page X/Y" on one line.
    display_lines = ["<b>📺 Display</b>"]
    if state["title"]:
        line = f"<b>{html.escape(state['title'])}</b>"
        src = source_name(state.get("url"))
        if src:
            url = state.get("url") or ""
            if url.startswith("http://") or url.startswith("https://"):
                src_html = (
                    f"<a href=\"{html.escape(url)}\">{html.escape(src)}</a>"
                )
            else:
                # Named cookbook URL: no link, just the human label.
                src_html = html.escape(src)
            line += f" <i>from {src_html}</i>"
        if state["total_pages"] > 1:
            line += f" — page {state['page']}/{state['total_pages']}"
        display_lines.append(line)
    else:
        display_lines.append(html.escape(state["type"]))
    sections.append("\n".join(display_lines))

    # Repertoire section
    library_lines = [f"<b>📚 Repertoire</b>", f"{library.count_saved()} saved recipes"]
    if backup.is_enabled():
        last_ts = backup.get_last_backup_at()
        backup_text = humanize_ago(last_ts) if last_ts else "never"
        library_lines.append(f"Last backup: {backup_text}")
    sections.append("\n".join(library_lines))

    # LLM section — headline only, mirrors what /app/status shows. Hidden
    # entirely when the LLM isn't configured (the section would just be
    # noise on a deployment that doesn't use it).
    if LLM_API_URL and LLM_API_KEY:
        month_start = int(
            datetime.now(TZ)
            .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        llm_stats = library.llm_month_stats(month_start)
        if llm_stats["calls"] == 0:
            sections.append("<b>🧠 LLM</b> — no calls this month yet")
        else:
            chf_prefix = "≥ " if llm_stats["chf_partial"] else "~"
            sections.append(
                f"<b>🧠 LLM</b> — {llm_stats['calls']} calls this month "
                f"({llm_stats['url_calls']} URL, {llm_stats['ocr_calls']} OCR, "
                f"{llm_stats['translate_calls']} translate), "
                f"{chf_prefix}CHF {llm_stats['chf']:.2f}"
            )

    # Device section — header carries freshness so the rows can be tight.
    # Fields are "as of last wake" (button press or daily timer).
    if device["last_seen"]:
        stale_suffix = (
            " ⚠️ overdue"
            if int(time.time()) - device["last_seen"] > device_telemetry.STALE_HEARTBEAT_S
            else ""
        )
        device_lines = [
            f"<b>📡 Device</b> — {humanize_ago(device['last_seen'])}{stale_suffix}"
        ]
        if device["battery_mv"]:
            pct = battery_pct(device["battery_mv"])
            icon = "🔋" if pct >= 30 else "🪫"
            device_lines.append(
                f"{icon} Battery: {pct}% ({device['battery_mv'] / 1000:.2f} V)"
            )
        if device.get("rssi"):
            rssi = device["rssi"]
            device_lines.append(f"📶 Signal: {rssi} dBm ({rssi_quality(rssi)})")
        if device.get("temperature_c") is not None:
            device_lines.append(f"🌡 Temp: {device['temperature_c']:.1f} °C")
        if device.get("humidity_pct") is not None:
            device_lines.append(f"💧 Humidity: {device['humidity_pct']:.0f} %")
        sections.append("\n".join(device_lines))
    else:
        sections.append("<b>📡 Device</b> — never seen")

    return "\n\n".join(sections)


async def cmd_status(update: Update, context) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(_build_status_text(), parse_mode="HTML")


async def cmd_comment(update: Update, context) -> None:
    """Add a comment to the recipe currently on the display.

    Three branches:
      - Nothing showing → tell the user to push something first.
      - Already-saved recipe → append the note immediately (the original
        path; doesn't bump displayed_count so adding a note isn't a "cook").
      - Pushed-but-unsaved recipe → offer a single tap to save it + add
        the note in one go, so users don't have to backtrack through the
        push message to find the 💾 Save button.
    """
    if not _is_allowed(update.effective_user.id):
        return

    state = display_state.get()

    if state["type"] != "recipe":
        await update.message.reply_text(
            "No recipe on the display — push one first, then add notes with /comment."
        )
        return

    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await update.message.reply_text(
            "Usage: <code>/comment &lt;text&gt;</code> — add a note to the "
            "recipe currently on the panel.",
            parse_mode="HTML",
        )
        return

    # Cap before storing/echoing — a multi-KB clipboard paste would
    # otherwise blow past Telegram's 4096-char per-message limit when
    # echoed back, raising BadRequest and orphaning the _pending_notes
    # entry. Trim with an ellipsis marker so the saved note and the
    # echoed confirmation agree on what was kept.
    if len(text) > _COMMENT_MAX_CHARS:
        text = text[:_COMMENT_MAX_CHARS - 1] + "…"

    recipe_id = state.get("recipe_id")
    if recipe_id is None:
        # Pushed-but-unsaved branch: chain save + comment via one button.
        url = state.get("url")
        pending_token = _find_pending_token_for_url(url)
        if pending_token is None:
            await update.message.reply_text(
                "This recipe's save session expired — re-paste the URL, then /comment again."
            )
            return
        _pending_notes[pending_token] = text
        await update.message.reply_text(
            "This recipe isn't saved yet. Tap below to save it and add this note:\n\n"
            f"<i>{html.escape(text)}</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "💾 Save & add note", callback_data=f"save_note:{pending_token}"
                ),
                InlineKeyboardButton(
                    "❌ Cancel", callback_data=f"note:cancel:{pending_token}"
                ),
            ]]),
        )
        return

    # Saved-recipe branch: append immediately.
    if library.add_comment(recipe_id, text) is None:
        # Recipe vanished or was soft-deleted between push and /comment.
        await update.message.reply_text(
            "⚠️ That recipe is gone — push a saved one to the display first."
        )
        return
    log.info("Comment added to recipe %d (%d chars)", recipe_id, len(text))
    # Deliberately do NOT re-push: adding a note shouldn't count as a cook
    # event, so last_displayed_at / displayed_count stay put. The note
    # will land on the panel on the next real push.
    await update.message.reply_text(
        "📝 Note added. It'll show on the panel next time you display this recipe."
    )


def _cooked_label(row: dict) -> str:
    """Match the 'cooked N×, last <when>' / 'never cooked' phrasing used
    across the web repertoire cards so the bot's search results, surprise
    card, and status all describe the same recipe the same way. `<when>`
    is the same humanised phrase (`2 days ago`, `last week`, …) the web
    cards render via `humanize_date`."""
    if row.get("last_displayed_at"):
        last = humanize_date(row["last_displayed_at"])
        count = row.get("displayed_count") or 0
        return f"cooked {count}×, last {last}" if count > 1 else f"cooked {last}"
    return "never cooked"


def _render_search_page(
    query: str, offset: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Build the (text, keyboard) for a /search results page.

    Returns None when the requested page is empty AND `offset == 0` (the
    "no matches at all" case — callers send a different message). Non-zero
    offsets with no rows still return a body so the user sees they've
    walked off the end.
    """
    results = library.search(query, limit=_SEARCH_PAGE_SIZE + 1, offset=offset)
    has_more = len(results) > _SEARCH_PAGE_SIZE
    results = results[:_SEARCH_PAGE_SIZE]

    if not results and offset == 0:
        return None

    # Always stash a token so the per-result preview buttons can carry the
    # (query, offset) context back to /search when the user taps « Back.
    token = _stash_search(query)

    page_num = offset // _SEARCH_PAGE_SIZE + 1
    header = f"🔍 <b>Matches for \"{html.escape(query)}\"</b>"
    if offset > 0 or has_more:
        header += f"  ·  page {page_num}"
    lines: list[str] = [header, ""]
    preview_buttons: list[InlineKeyboardButton] = []
    for i, r in enumerate(results, start=offset + 1):
        title = html.escape(r["title"])
        src_html = _format_source_html(r.get("url"))
        item_header = f"<b>{i}.</b> {title}"
        if src_html:
            item_header += f" {src_html}"
        lines.append(item_header)
        lines.append(f"<i>   {_cooked_label(r)}</i>")
        lines.append("")
        # Preview (don't push yet) — back-button needs token + offset to
        # rebuild this exact page.
        preview_buttons.append(InlineKeyboardButton(
            str(i),
            callback_data=f"search_preview:{token}:{offset}:{r['id']}",
        ))

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    if preview_buttons:
        keyboard_rows.append(preview_buttons)

    nav_row: list[InlineKeyboardButton] = []
    if offset > 0 or has_more:
        if offset > 0:
            prev_offset = max(0, offset - _SEARCH_PAGE_SIZE)
            nav_row.append(InlineKeyboardButton(
                "« Prev", callback_data=f"search:{token}:{prev_offset}"
            ))
        if has_more:
            nav_row.append(InlineKeyboardButton(
                "Next »",
                callback_data=f"search:{token}:{offset + _SEARCH_PAGE_SIZE}",
            ))
    if nav_row:
        keyboard_rows.append(nav_row)

    body = "\n".join(lines).rstrip()
    if not results:
        body += "\n\n<i>No more matches.</i>"
    return body, InlineKeyboardMarkup(keyboard_rows)


async def cmd_search(update: Update, context) -> None:
    """Full-text search the saved recipe repertoire; tap a result to push it."""
    if not _is_allowed(update.effective_user.id):
        return

    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text(
            "Usage: <code>/search &lt;query&gt;</code> — full-text search "
            "across saved recipes.",
            parse_mode="HTML",
        )
        return

    rendered = _render_search_page(query, offset=0)
    if rendered is None:
        await update.message.reply_text(
            f"No saved recipes match '{query}'. Try a shorter or different term."
        )
        return
    body, keyboard = rendered
    await update.message.reply_text(
        body,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


async def on_search_nav(update: Update, context) -> None:
    """User tapped « Prev / Next » under a /search result page."""
    query = update.callback_query
    if not _is_allowed(update.effective_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return
    try:
        _, token, offset_str = query.data.split(":")
        offset = int(offset_str)
    except (ValueError, IndexError):
        await query.answer("Bad callback.", show_alert=True)
        return
    query_text = _search_queries.get(token)
    if not query_text:
        await query.answer(
            "Search session expired — re-run /search.", show_alert=True
        )
        return
    rendered = _render_search_page(query_text, offset=max(0, offset))
    if rendered is None:
        await query.answer("No matches.", show_alert=True)
        return
    body, keyboard = rendered
    await query.answer()
    try:
        await query.edit_message_text(
            body,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
    except Exception:
        log.exception("on_search_nav: failed to edit message")


def _format_search_preview(row: dict) -> str:
    """Preview card for a /search result — mirrors the surprise card style.

    Shown after tapping a numbered result button so the user can confirm
    before committing the push (a misclick on the bare number used to
    overwrite the panel instantly).
    """
    title = html.escape(row["title"])
    body = f"🔍 <b>{title}</b>"
    src_html = _format_source_html(row.get("url"))
    if src_html:
        body += f" {src_html}"
    body += f"\n<i>{_cooked_label(row)}</i>"
    return body


def _search_preview_keyboard(recipe_id: int, token: str, offset: int) -> InlineKeyboardMarkup:
    """[Push] / [Back] row under a search preview card.

    Push commits via the existing `push:` handler. Back carries the token +
    offset so the search results page can be re-rendered exactly as the
    user left it.
    """
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📺 Push", callback_data=f"push:{recipe_id}"),
        InlineKeyboardButton("« Back", callback_data=f"search_back:{token}:{offset}"),
    ]])


async def on_search_preview(update: Update, context) -> None:
    """User tapped a numbered /search result — show a preview before pushing."""
    query = update.callback_query
    if not _is_allowed(update.effective_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return
    try:
        _, token, offset_str, recipe_id_str = query.data.split(":")
        offset = int(offset_str)
        recipe_id = int(recipe_id_str)
    except (ValueError, IndexError):
        await query.answer("Bad callback.", show_alert=True)
        return
    row = library.get_recipe(recipe_id)
    if row is None:
        await query.answer("Recipe missing — was it deleted?", show_alert=True)
        return
    await query.answer()
    try:
        await query.edit_message_text(
            _format_search_preview(row),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_search_preview_keyboard(row["id"], token, offset),
        )
    except Exception:
        log.exception("on_search_preview: failed to edit message")


async def on_search_back(update: Update, context) -> None:
    """User tapped « Back from a search preview — re-render the result page."""
    query = update.callback_query
    if not _is_allowed(update.effective_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return
    try:
        _, token, offset_str = query.data.split(":")
        offset = int(offset_str)
    except (ValueError, IndexError):
        await query.answer("Bad callback.", show_alert=True)
        return
    query_text = _search_queries.get(token)
    if not query_text:
        await query.answer(
            "Search session expired — re-run /search.", show_alert=True
        )
        return
    rendered = _render_search_page(query_text, offset=max(0, offset))
    if rendered is None:
        await query.answer("No matches.", show_alert=True)
        return
    body, keyboard = rendered
    await query.answer()
    try:
        await query.edit_message_text(
            body,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
    except Exception:
        log.exception("on_search_back: failed to edit message")


def _format_surprise_card(row: dict) -> str:
    """The Surprise card body — title, source, and a cooked-history line.

    The 'Push' button is what commits the choice; the message itself is a
    preview so the user can re-roll without disturbing whatever's currently
    on the panel.
    """
    title = html.escape(row["title"])
    body = f"🎲 <b>{title}</b>"
    src_html = _format_source_html(row.get("url"))
    if src_html:
        body += f" {src_html}"
    body += f"\n<i>{_cooked_label(row)}</i>"
    return body


def _surprise_keyboard(recipe_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎲 Another", callback_data=f"surprise_again:{recipe_id}"),
        InlineKeyboardButton("📺 Push", callback_data=f"surprise_push:{recipe_id}"),
    ]])


async def cmd_surprise(update: Update, context) -> None:
    """Pick a random saved recipe and show it; user decides whether to push.

    Old behaviour pushed straight to the panel, which was annoying when
    the random pick wasn't the one you wanted — it'd already overwritten
    the previous content. Now the bot shows the pick with 🎲 Another and
    📺 Push buttons, so re-rolls don't touch the display.
    """
    if not _is_allowed(update.effective_user.id):
        return
    row = library.random_recipe()
    if row is None:
        await update.message.reply_text(
            "Your repertoire is empty — save a recipe first."
        )
        return
    log.info("Surprise picked: id=%d title=%r", row["id"], row["title"])
    await update.message.reply_text(
        _format_surprise_card(row),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=_surprise_keyboard(row["id"]),
    )


async def on_surprise_again(update: Update, context) -> None:
    """Re-roll the random pick in place, excluding the previous choice."""
    query = update.callback_query
    if not _is_allowed(update.effective_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return
    try:
        _, prev_id_str = query.data.split(":", 1)
        prev_id: int | None = int(prev_id_str)
    except (ValueError, IndexError):
        prev_id = None

    row = library.random_recipe(exclude_id=prev_id)
    if row is None:
        # Repertoire has only the excluded recipe — fall back to picking it.
        if prev_id is not None:
            row = library.get_recipe(prev_id)
        if row is None:
            await query.answer("Repertoire is empty.", show_alert=True)
            return
        await query.answer("Only one saved recipe.", show_alert=True)
    else:
        await query.answer()

    log.info("Surprise re-rolled: id=%d title=%r", row["id"], row["title"])
    try:
        await query.edit_message_text(
            _format_surprise_card(row),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_surprise_keyboard(row["id"]),
        )
    except Exception:
        log.exception("on_surprise_again: failed to edit message")


async def on_surprise_push(update: Update, context) -> None:
    """Commit the currently-shown surprise pick to the e-ink panel."""
    query = update.callback_query
    if not _is_allowed(update.effective_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return
    try:
        _, recipe_id_str = query.data.split(":", 1)
        recipe_id = int(recipe_id_str)
    except (ValueError, IndexError):
        await query.answer("Bad callback.", show_alert=True)
        return
    row = library.get_recipe(recipe_id)
    if row is None:
        await query.answer("Recipe gone — pick another.", show_alert=True)
        return
    if not push_recipe_to_display(row):
        await query.answer("Couldn't render that recipe.", show_alert=True)
        return
    total = display_state.get()["total_pages"]
    log.info("Surprise pushed: id=%d title=%r", row["id"], row["title"])
    await query.answer(f"Pushed: {row['title']}")
    # Edit the surprise card to a final pushed-confirmation, clearing the
    # buttons so the message reads as committed rather than mid-decision.
    pushed_body = (
        _format_surprise_card(row)
        + f"\n\n✅ Pushed to display"
        + (f" — {total} pages" if total > 1 else "")
    )
    try:
        await query.edit_message_text(
            pushed_body,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("on_surprise_push: failed to edit message")


async def on_push_button(update: Update, context) -> None:
    """User tapped 📺 Push under a /search preview — render and push that recipe."""
    query = update.callback_query
    if not _is_allowed(update.effective_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return
    try:
        _, recipe_id_str = query.data.split(":")
        recipe_id = int(recipe_id_str)
    except (ValueError, IndexError):
        await query.answer("Bad callback.", show_alert=True)
        return

    row = library.get_recipe(recipe_id)
    if row is None:
        await query.answer("Recipe missing — was it deleted?", show_alert=True)
        return

    if not push_recipe_to_display(row):
        await query.answer("Couldn't render that recipe.", show_alert=True)
        return
    total = display_state.get()["total_pages"]
    log.info("Search pushed: id=%d title=%r", row["id"], row["title"])
    await query.answer(f"Pushed: {row['title']}")
    # Mirror the surprise-push behaviour: replace the preview body with a
    # final committed-confirmation and drop the buttons so a stale tap
    # can't re-push.
    pushed_body = (
        _format_search_preview(row)
        + f"\n\n✅ Pushed to display"
        + (f" — {total} pages" if total > 1 else "")
    )
    try:
        await query.edit_message_text(
            pushed_body,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("on_push_button: failed to edit message")


@asynccontextmanager
async def _typing_indicator(bot, chat_id: int):
    """Keep Telegram's 'typing…' indicator alive for the duration of the block.

    The Bot API clears `sendChatAction` after ~5 s, so we re-ping every 4 s
    while inside the context. Cancel on exit so the indicator vanishes when
    the work finishes. Useful on the OCR / LLM-fallback paths where a single
    handler call can sit for 20-30 s and the user would otherwise wonder if
    the bot froze.
    """
    async def _loop():
        try:
            while True:
                try:
                    await bot.send_chat_action(chat_id=chat_id, action="typing")
                except Exception:
                    # Network blip or rate-limit — keep looping; the next
                    # ping will recover. Don't surface to the caller, the
                    # indicator is purely a UX nicety.
                    log.debug("typing indicator ping failed", exc_info=True)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            return

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def on_photo(update: Update, context) -> None:
    """Handle photo messages — OCR via LLM, then push the recipe to the display.

    Falls through to `_present_result` so the result lands in exactly the
    same Save-button flow as a pasted URL.
    """
    if not _is_allowed(update.effective_user.id):
        return

    log.info("Photo received from user %s", update.effective_user.id)
    msg = await update.message.reply_text("🤖 Converting the recipe with an LLM…")

    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        image_bytes = await file.download_as_bytearray()
    except Exception:
        log.exception("Photo download failed")
        await msg.edit_text("❌ Couldn't download the photo. Try again.")
        return

    # Caption travels with the photo to the LLM as a "User context" hint.
    # Typical use: "Ottolenghi Simple" or "from my mother's recipe book"
    # — anything that helps the OCR fill source_name when the photo
    # itself doesn't show the cover.
    hint = (update.message.caption or "").strip() or None

    # OCR is always an LLM call — wrap it in the typing indicator so the
    # 10-30 s wait reads as "bot is working" instead of "did this freeze?".
    async with _typing_indicator(context.bot, msg.chat_id):
        try:
            result = await ingest_recipe(
                bytes(image_bytes), push=True, persist=False, hint=hint,
            )
        except IngestError:
            await msg.edit_text("❌ Couldn't read a recipe from that photo.")
            return

    log.info(
        "Photo OCR ingested: title=%r url=%s action=%s",
        result["recipe"].get("title"), result["url"], result["action"],
    )
    await _present_result(result, msg)


async def on_text(update: Update, context) -> None:
    """Handle text messages — check if it's a URL."""
    if not _is_allowed(update.effective_user.id):
        return

    text = update.message.text.strip()

    # Check if it looks like a URL
    if not (text.startswith("http://") or text.startswith("https://")):
        await update.message.reply_text(
            "Send me a photo, a recipe URL, or use /help"
        )
        return

    log.info("Received URL from user %s: %s", update.effective_user.id, text)
    msg = await update.message.reply_text("🔍 Fetching recipe...")
    await _fetch_and_display_recipe(text, msg)


async def _fetch_and_display_recipe(url: str, msg) -> None:
    """Fetch a recipe URL, render all pages, push to display, and reply.

    The placeholder reply starts as "🔍 Fetching recipe…". If the URL
    falls through to the LLM, the placeholder is edited once to make the
    longer wait legible — and the typing indicator runs throughout so
    Telegram shows "Bot is typing" the whole time.
    """
    async def _on_llm_start() -> None:
        try:
            await msg.edit_text("🤖 Converting the recipe with an LLM…")
        except Exception:
            log.debug("placeholder edit on LLM start failed", exc_info=True)

    async with _typing_indicator(msg.get_bot(), msg.chat_id):
        try:
            result = await ingest_recipe(
                url, push=True, persist=False, on_llm_start=_on_llm_start,
            )
        except IngestError:
            log.warning("Failed to parse recipe from URL: %s", url)
            await msg.edit_text("❌ Couldn't read a recipe from that URL.")
            return
    await _present_result(result, msg)


def _push_inline_actions(
    recipe_id: int | None, pending_token: str | None
) -> InlineKeyboardMarkup | None:
    """Inline buttons that follow a push confirmation.

    Layout differs by state:
      - Unsaved (pending_token set): 💾 Save first, optionally 🌐 Web link
        to the Add page so the user can re-route to the web flow.
      - Already-saved (recipe_id set): 🌐 Web link to the recipe detail
        page so notes / scaling / source-original-link are one tap away.
    Returns None when there'd be no useful buttons to add (e.g. saved
    recipe and no WEB_URL configured).
    """
    row: list[InlineKeyboardButton] = []
    if pending_token is not None:
        row.append(InlineKeyboardButton(
            "💾 Save", callback_data=f"save:{pending_token}"
        ))
    if WEB_URL and recipe_id is not None:
        row.append(InlineKeyboardButton(
            "🌐 Open in web",
            url=f"{WEB_URL}/app/recipes/{recipe_id}",
        ))
    if not row:
        return None
    return InlineKeyboardMarkup([row])


async def _present_result(result: dict, msg) -> None:
    """Render the ingest_recipe outcome back into the Telegram message.

    Two states:
      - Already-saved (`recipe_id` set): no Save button, just the web
        link for one-tap access to notes / scaling.
      - New URL (`recipe_id is None`): stash the parsed dict in the
        pending map and offer 💾 Save — the library row is only created
        when the user taps it. Pending-stash is Telegram-specific UX, so
        it lives here rather than inside ingest_recipe.

    A "parsed-only" action means push failed (parse succeeded but the
    e-ink render raised). Surface the render error to the user.
    """
    if result["action"] == "parsed-only":
        await msg.edit_text("❌ Couldn't render that recipe to the display.")
        return

    url = result["url"]
    recipe = result["recipe"]
    recipe_id = result["recipe_id"]
    state = display_state.get()
    total_pages = state["total_pages"]

    if recipe_id is not None:
        # Already in the library — ingest_recipe pushed via the saved
        # row, with its real comments + touch_displayed bump. Title comes
        # from display_state so the message echoes what landed on the
        # panel (the saved row, possibly stale vs. the re-parsed dict).
        await msg.edit_text(
            _format_push_reply(state.get("title", ""), url, total_pages),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_push_inline_actions(
                recipe_id=recipe_id, pending_token=None,
            ),
        )
        return

    # New URL / unseen photo — stash the parsed dict so the 💾 Save
    # callback can persist on demand.
    token = _stash_pending(url, recipe)
    await msg.edit_text(
        _format_push_reply(recipe["title"], url, total_pages),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=_push_inline_actions(recipe_id=None, pending_token=token),
    )


def _format_source_html(url: str | None) -> str:
    """Render '<i>from Source</i>' (linkified for http(s) URLs) or '' if no source.

    Mirrors the style used by cmd_status so the bot's push confirmations,
    search results, and status panel all describe a recipe's origin the
    same way.
    """
    src = source_name(url) if url else None
    if not src:
        return ""
    if url and (url.startswith("http://") or url.startswith("https://")):
        src_html = f"<a href=\"{html.escape(url)}\">{html.escape(src)}</a>"
    else:
        src_html = html.escape(src)
    return f"<i>from {src_html}</i>"


def _format_push_reply(title: str, url: str | None, total_pages: int) -> str:
    """Two-line confirmation for a pushed recipe (HTML, matches /status style)."""
    body = f"✅ <b>{html.escape(title)}</b>"
    src_html = _format_source_html(url)
    if src_html:
        body += f" {src_html}"
    if total_pages > 1:
        body += f"\n📄 {total_pages} pages"
    return body


async def on_save_button(update: Update, context) -> None:
    """User tapped 💾 Save — persist the recipe to the repertoire."""
    query = update.callback_query
    if not _is_allowed(update.effective_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return
    try:
        _, token = query.data.split(":", 1)
    except ValueError:
        log.warning("on_save_button: malformed callback data %r", query.data)
        await query.answer("Bad callback.", show_alert=True)
        return

    pending = _pending.pop(token, None)
    # Drop any orphaned note that was queued for this token; we're not
    # routing through the save_note branch so the note would be stranded.
    _pending_notes.pop(token, None)
    if pending is None:
        await query.answer("Session expired — repush the URL to save.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    url, recipe = pending
    translated = await translate_for_search(recipe)
    recipe_id = library.upsert_recipe(
        url, recipe,
        translated_keywords=translated,
    )
    library.save_recipe(recipe_id)
    log.info("Bot save: id=%d title=%r", recipe_id, recipe.get("title"))

    await query.answer("💾 Saved")
    # Swap the keyboard to surface the next useful action — web link if
    # configured — instead of leaving a bare confirmation. The Save button
    # itself is gone (already saved) so a stale tap can't double-save.
    new_markup = _push_inline_actions(recipe_id=recipe_id, pending_token=None)
    try:
        await query.edit_message_reply_markup(reply_markup=new_markup)
    except Exception:
        pass
    if query.message is not None:
        await query.message.reply_text("💾 Saved to repertoire.")


async def on_save_note_button(update: Update, context) -> None:
    """User tapped 💾 Save & add note — commit both in a single action.

    Triggered from cmd_comment's pushed-but-unsaved branch: the user typed
    `/comment foo` while a not-yet-saved recipe was on the display, and
    this button chains the save + the note together.
    """
    query = update.callback_query
    if not _is_allowed(update.effective_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return
    try:
        _, token = query.data.split(":", 1)
    except ValueError:
        log.warning("on_save_note_button: malformed callback data %r", query.data)
        await query.answer("Bad callback.", show_alert=True)
        return

    pending = _pending.pop(token, None)
    note = _pending_notes.pop(token, None)
    if pending is None or note is None:
        await query.answer("Session expired — re-run /comment.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    url, recipe = pending
    translated = await translate_for_search(recipe)
    recipe_id = library.upsert_recipe(
        url, recipe,
        translated_keywords=translated,
    )
    library.save_recipe(recipe_id)
    library.add_comment(recipe_id, note)
    log.info(
        "Bot save+note: id=%d title=%r note_len=%d",
        recipe_id, recipe.get("title"), len(note),
    )
    await query.answer("💾 Saved with note")
    new_markup = _push_inline_actions(recipe_id=recipe_id, pending_token=None)
    try:
        await query.edit_message_reply_markup(reply_markup=new_markup)
    except Exception:
        pass
    if query.message is not None:
        await query.message.reply_text("💾 Saved and 📝 note added.")


async def on_note_cancel(update: Update, context) -> None:
    """User tapped ❌ Cancel on a pending /comment note — discard the stash."""
    query = update.callback_query
    if not _is_allowed(update.effective_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return
    try:
        _, _, token = query.data.split(":", 2)
    except ValueError:
        await query.answer("Bad callback.", show_alert=True)
        return
    _pending_notes.pop(token, None)
    await query.answer()
    try:
        await query.edit_message_text("Cancelled.")
    except Exception:
        log.exception("on_note_cancel: failed to edit message")


async def on_quick_action(update: Update, context) -> None:
    """Quick-action buttons under /help — sidesteps typing slash commands."""
    query = update.callback_query
    if not _is_allowed(update.effective_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return
    try:
        _, action = query.data.split(":", 1)
    except ValueError:
        await query.answer("Bad callback.", show_alert=True)
        return
    await query.answer()
    chat_id = query.message.chat_id if query.message else update.effective_chat.id

    if action == "status":
        # Reuse the same body cmd_status builds; cheaper than parameterising
        # cmd_status itself, and keeps the two surfaces obviously parallel.
        await context.bot.send_message(
            chat_id=chat_id,
            text=_build_status_text(),
            parse_mode="HTML",
        )
    elif action == "surprise":
        row = library.random_recipe()
        if row is None:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Your repertoire is empty — save a recipe first.",
            )
            return
        await context.bot.send_message(
            chat_id=chat_id,
            text=_format_surprise_card(row),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_surprise_keyboard(row["id"]),
        )
