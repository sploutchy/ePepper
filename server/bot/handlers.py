"""Telegram bot handlers for ePepper."""

import html
import logging
import re
import time
import uuid
from collections import OrderedDict
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
    TELEGRAM_BOT_TOKEN,
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

# Short tokens → search queries, so paginated /search buttons can carry a
# 6-char ref in their 64-byte callback_data instead of stuffing the full
# query (and risking truncation / encoding issues).
_SEARCH_QUERIES_MAX = 32
_search_queries: "OrderedDict[str, str]" = OrderedDict()
_SEARCH_PAGE_SIZE = 5

# Matches the first http(s) URL anywhere in a message, so a link pasted
# mid-sentence ("try this https://… yum") is still recognised. Trailing
# sentence punctuation is trimmed off the captured URL in on_text.
_URL_RE = re.compile(r"https?://\S+")


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
    report comes in below the threshold (device_telemetry owns the once-per-episode flag).
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


def _stash_pending(url: str, recipe: dict) -> str:
    token = uuid.uuid4().hex[:8]
    _pending[token] = (url, recipe)
    while len(_pending) > _PENDING_MAX:
        _pending.popitem(last=False)
    return token


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
    ("search", "Find a saved recipe"),
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
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_save_button, pattern=r"^save:"))
    app.add_handler(CallbackQueryHandler(on_push_button, pattern=r"^push:"))
    app.add_handler(CallbackQueryHandler(on_search_nav, pattern=r"^search:"))
    # Catch-all for unknown /commands — must register after every named
    # CommandHandler so known commands match first.
    app.add_handler(MessageHandler(filters.COMMAND, on_unknown_command))

    return app


async def on_unknown_command(update: Update, context) -> None:
    if not update.effective_user or not _is_allowed(update.effective_user.id):
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
    """One-line linkified pointer to the web app (HTML), or "" when WEB_URL is unset."""
    if not WEB_URL:
        return ""
    return (
        f"🌐 <b>Web app:</b> "
        f"<a href=\"{html.escape(WEB_URL)}/app/\">{html.escape(WEB_URL)}/app/</a> "
        "(same API_KEY logs you in) — sort, filter, and browse the full repertoire."
    )


async def cmd_start(update: Update, context) -> None:
    if not _is_allowed(update.effective_user.id):
        log.info(f"User {update.effective_user.id} is not in the ALLOWED_USERS list")
        return
    # /start shows the same reference as /help — one canonical text.
    await cmd_help(update, context)


# Sectioned help — cleaner skim than one long wall of slash commands.
# Each section starts with a one-line "what this is for" then lists the
# commands that belong to it. The native /-menu (set via set_my_commands)
# covers the most-used ones, so /help is the deep reference.
_HELP_TEXT = (
    "🫑 <b>ePepper — help</b>\n\n"
    "<b>➕ Add a recipe</b>\n"
    "Paste a link or send a photo of a cookbook / magazine page — I'll "
    "push it to the panel.\n"
    "<i>Use the web app to save without displaying.</i>\n\n"
    "<b>📚 Repertoire</b>\n"
    "Tap 💾 Save under a push to keep a recipe.\n"
    "  /search &lt;query&gt; — find a saved recipe (paginated)\n\n"
    "<b>📺 Display</b>\n"
    "Physical buttons cycle pages.\n"
    "  /clear — clear the panel\n\n"
    "<b>ℹ️ Info</b>\n"
    "  /status — device + repertoire snapshot\n"
)


async def cmd_help(update: Update, context) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    web = _web_app_line()
    text = _HELP_TEXT + (f"\n{web}\n" if web else "")
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def cmd_clear(update: Update, context) -> None:
    """Clear the e-ink panel immediately."""
    if not _is_allowed(update.effective_user.id):
        return
    display_state.clear()
    await update.message.reply_text("🧹 Display cleared.")


def _build_status_text() -> str:
    """Render the /status sectioned snapshot as a single HTML string."""
    state = display_state.get()
    device = device_telemetry.get_device_status()

    sections = ["🫑 <b>ePepper Status</b>"]

    # Display section — "<b>title</b> from <source> — page X/Y" on one line.
    display_lines = ["<b>📺 Display</b>"]
    if state["title"]:
        line = f"<b>{html.escape(state['title'])}</b>"
        src_html = _format_source_html(state.get("url"))
        if src_html:
            line += f" {src_html}"
        if state["total_pages"] > 1:
            line += f" — page {state['page']}/{state['total_pages']}"
        display_lines.append(line)
    else:
        display_lines.append(html.escape(state["type"]))
    sections.append("\n".join(display_lines))

    # Repertoire section
    library_lines = ["<b>📚 Repertoire</b>", f"{library.count_saved()} saved recipes"]
    if backup.is_enabled():
        last_ts = backup.get_last_backup_at()
        backup_text = humanize_ago(last_ts) if last_ts else "never"
        library_lines.append(f"Last backup: {backup_text}")
    sections.append("\n".join(library_lines))

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


def _cooked_label(row: dict) -> str:
    if row.get("last_displayed_at"):
        return f"cooked {humanize_date(row['last_displayed_at'])}"
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

    # Always stash a token so the « Prev / Next » buttons can carry the
    # (query, offset) context back to on_search_nav.
    token = _stash_search(query)

    page_num = offset // _SEARCH_PAGE_SIZE + 1
    header = f"🔍 <b>Matches for \"{html.escape(query)}\"</b>"
    if offset > 0 or has_more:
        header += f"  ·  page {page_num}"
    lines: list[str] = [header, ""]
    push_buttons: list[InlineKeyboardButton] = []
    for i, r in enumerate(results, start=offset + 1):
        title = html.escape(r["title"])
        src_html = _format_source_html(r.get("url"))
        item_header = f"<b>{i}.</b> {title}"
        if src_html:
            item_header += f" {src_html}"
        lines.append(item_header)
        lines.append(f"<i>   {_cooked_label(r)}</i>")
        lines.append("")
        # Tapping a numbered result pushes it straight to the panel.
        push_buttons.append(InlineKeyboardButton(
            str(i),
            callback_data=f"push:{r['id']}",
        ))

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    if push_buttons:
        keyboard_rows.append(push_buttons)

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


async def on_push_button(update: Update, context) -> None:
    """User tapped a numbered /search result — render and push that recipe."""
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
    # Replace the results list with a final committed-confirmation and drop
    # the buttons so a stale tap can't re-push.
    title = html.escape(row["title"])
    pushed_body = f"🔍 <b>{title}</b>"
    src_html = _format_source_html(row.get("url"))
    if src_html:
        pushed_body += f" {src_html}"
    pushed_body += "\n\n✅ Pushed to display"
    if total > 1:
        pushed_body += f" — {total} pages"
    try:
        await query.edit_message_text(
            pushed_body,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("on_push_button: failed to edit message")


async def on_photo(update: Update, context) -> None:
    """Handle photo messages — OCR via LLM, then push the recipe to the display.

    Falls through to `_present_result` so the result lands in exactly the
    same Save-button flow as a pasted URL.
    """
    if not update.effective_user or not _is_allowed(update.effective_user.id):
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
    """Route a free-text message.

    A message containing an http(s) link → fetch + push it to the panel
    immediately. Anything else → a short hint pointing at the real inputs.
    """
    if not update.effective_user or not _is_allowed(update.effective_user.id):
        return

    text = update.message.text.strip()
    if not text:
        return

    match = _URL_RE.search(text)
    if match:
        url = match.group(0).rstrip(").,;:!?'\"")
        log.info("URL detected from user %s: %s", update.effective_user.id, url)
        msg = await update.message.reply_text("🔍 Fetching recipe...")
        await _fetch_and_display_recipe(url, msg)
        return

    await update.message.reply_text(
        "Send me a photo or a recipe URL, or use /help."
    )


async def _fetch_and_display_recipe(url: str, msg) -> None:
    """Fetch a recipe URL, render all pages, push to display, and reply.

    The placeholder reply starts as "🔍 Fetching recipe…". If the URL
    falls through to the LLM, the placeholder is edited once to make the
    longer wait legible.
    """
    async def _on_llm_start() -> None:
        try:
            await msg.edit_text("🤖 Converting the recipe with an LLM…")
        except Exception:
            log.debug("placeholder edit on LLM start failed", exc_info=True)

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
        # row, with its touch_displayed bump. Title comes from
        # display_state so the message echoes what landed on the panel
        # (the saved row, possibly stale vs. the re-parsed dict).
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
        source=source_name(url),
    )
    library.save_recipe(recipe_id)
    # The pending-save flow only exists for recipes that were already
    # pushed to the panel transiently (recipe_id=None at push time, see
    # ingest_recipe) — touch_displayed never fired for them, so the row
    # would otherwise be born with last_displayed_at still NULL despite
    # having just been shown.
    library.touch_displayed(recipe_id)
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
