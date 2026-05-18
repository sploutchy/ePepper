"""Telegram bot handlers for ePepper."""

import hashlib
import json
import logging
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from typing import Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from config import TELEGRAM_BOT_TOKEN, ALLOWED_USERS
import display_state
import library
from processing.images import process_photo
from processing.jsonld import parse_recipe_jsonld
from processing.recipes import process_recipe_url

# Hard cap on uploaded JSON size. Schema.org Recipe payloads are typically
# a few KB; anything larger is almost certainly not a recipe.
_JSON_MAX_BYTES = 256 * 1024

log = logging.getLogger(__name__)


# Pending unsaved recipes, keyed by a short token embedded in the Save
# button's callback_data. The map only holds parsed recipes the user has
# not yet rated; once a star is tapped (or 32 newer pushes have arrived)
# the entry is removed.
_PENDING_MAX = 32
_pending: "OrderedDict[str, Tuple[str, dict]]" = OrderedDict()


# Set by create_bot() so out-of-band code paths (e.g. low-battery alerts
# from the /device/status endpoint) can push messages without needing the
# Application instance threaded through.
_bot_app: Application | None = None


async def notify_low_battery(battery_mv: int) -> None:
    """Push a one-shot low-battery warning to every allowed user.

    Called by the FastAPI /device/status handler the first time a wake-cycle
    report comes in below the threshold (display_state owns the hysteresis).
    Silent when no users are configured — we don't have anywhere to send.
    """
    if _bot_app is None:
        log.warning("notify_low_battery: bot not yet initialised")
        return
    if not ALLOWED_USERS:
        log.warning("notify_low_battery: no ALLOWED_USERS configured, skipping alert")
        return
    text = (
        f"⚠️ ePepper battery is low: {battery_mv / 1000:.2f} V — charge soon."
    )
    for uid in ALLOWED_USERS:
        try:
            await _bot_app.bot.send_message(chat_id=uid, text=text)
            log.info("Low-battery alert sent to user %s (%dmV)", uid, battery_mv)
        except Exception:
            log.exception("Failed to send low-battery alert to user %s", uid)


def _stash_pending(url: str, recipe: dict) -> str:
    token = uuid.uuid4().hex[:8]
    _pending[token] = (url, recipe)
    while len(_pending) > _PENDING_MAX:
        _pending.popitem(last=False)
    return token


def create_bot() -> Application:
    """Create and configure the Telegram bot application."""
    global _bot_app
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    _bot_app = app

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("recipe", cmd_recipe))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("comment", cmd_comment))
    app.add_handler(CommandHandler("rate", cmd_rate))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(
        filters.Document.FileExtension("json") | filters.Document.MimeType("application/json"),
        on_document,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_save_button, pattern=r"^save:"))
    app.add_handler(CallbackQueryHandler(on_rate_button, pattern=r"^rate:"))
    app.add_handler(CallbackQueryHandler(on_push_button, pattern=r"^push:"))

    return app


def _is_allowed(user_id: int) -> bool:
    """Check if user is allowed (empty list = allow all)."""
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


async def cmd_start(update: Update, context) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "🌶️ *ePepper* — your kitchen recipe display\n\n"
        "Send me:\n"
        "• A *photo* of a recipe\n"
        "• A *recipe URL* (just paste the link)\n"
        "• A *.json file* with a schema.org Recipe (for OCR / unsupported sites — "
        "have an LLM produce the JSON-LD and upload it)\n"
        "• `/recipe <url>` to force recipe parsing\n\n"
        "Commands:\n"
        "/comment <text> — add a note to a saved recipe (must save first)\n"
        "/rate <1-5> — change the rating of the displayed saved recipe\n"
        "/search <query> — find a saved recipe by title, ingredient, or note\n"
        "/clear — clear the display\n"
        "/status — device info\n"
        "/help — this message\n\n"
        "Tap *💾 Save* under a pushed recipe to keep it in your library "
        "(rate 1–5 stars to confirm). Use the *device's physical buttons* "
        "to cycle between recipe pages.",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    await cmd_start(update, context)


async def cmd_recipe(update: Update, context) -> None:
    """Parse a recipe URL and display it."""
    if not _is_allowed(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: `/recipe <url>`", parse_mode="Markdown")
        return

    url = context.args[0]
    log.info("Recipe command from user %s: %s", update.effective_user.id, url)
    msg = await update.message.reply_text("🔍 Fetching recipe...")
    await _fetch_and_display_recipe(url, msg)


async def cmd_clear(update: Update, context) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    display_state.clear()
    await update.message.reply_text("🧹 Display cleared.")


async def cmd_status(update: Update, context) -> None:
    if not _is_allowed(update.effective_user.id):
        return

    state = display_state.get()
    device = display_state.get_device_status()

    # All device-reported fields are "as of last button-press wake" — the
    # firmware does not poll on a schedule, so freshness == last interaction.
    lines = [
        "📊 *ePepper Status*",
        "",
        f"Display: {state['type']}",
    ]
    if state["title"]:
        lines.append(f"Recipe: {state['title']}")
    if state["total_pages"] > 1:
        lines.append(f"Page: {state['page']}/{state['total_pages']}")

    if device["last_seen"]:
        ago = int(time.time() - device["last_seen"])
        lines.append(f"Last seen: {ago}s ago")
        if device["battery_mv"]:
            lines.append(f"Battery: {device['battery_mv'] / 1000:.2f} V")
        if device.get("temperature_c") is not None:
            lines.append(f"Temp: {device['temperature_c']:.1f} °C")
        if device.get("humidity_pct") is not None:
            lines.append(f"Humidity: {device['humidity_pct']:.0f} %")
    else:
        lines.append("Last seen: never")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_comment(update: Update, context) -> None:
    """Add a comment to the currently-displayed *saved* recipe."""
    if not _is_allowed(update.effective_user.id):
        return

    state = display_state.get()
    recipe_id = state.get("recipe_id")

    if state["type"] != "recipe" or recipe_id is None:
        await update.message.reply_text(
            "Save the recipe first (tap *💾 Save* under the push message), "
            "then add notes with /comment.",
            parse_mode="Markdown",
        )
        return

    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await update.message.reply_text(
            "Usage: `/comment <your note>`", parse_mode="Markdown"
        )
        return

    library.add_comment(recipe_id, text)
    log.info("Comment added to recipe %d (%d chars)", recipe_id, len(text))

    row = library.get_recipe(recipe_id)
    if row is None:
        await update.message.reply_text("⚠️ Couldn't reload the recipe.")
        return

    push_recipe_to_display(row)
    total = display_state.get()["total_pages"]
    await update.message.reply_text(
        f"📝 Note added — recipe now spans {total} page{'s' if total != 1 else ''}."
    )


async def cmd_rate(update: Update, context) -> None:
    """Change the rating of the currently-displayed saved recipe."""
    if not _is_allowed(update.effective_user.id):
        return

    state = display_state.get()
    recipe_id = state.get("recipe_id")

    if state["type"] != "recipe" or recipe_id is None:
        await update.message.reply_text(
            "Push a saved recipe to the display first, then use /rate to change its rating.",
        )
        return

    arg = context.args[0] if context.args else ""
    try:
        rating = int(arg)
    except ValueError:
        rating = 0
    if not 1 <= rating <= 5:
        await update.message.reply_text("Usage: `/rate <1-5>`", parse_mode="Markdown")
        return

    library.mark_saved(recipe_id, rating)
    log.info("Rating updated for recipe %d → %d", recipe_id, rating)

    row = library.get_recipe(recipe_id)
    if row is None:
        await update.message.reply_text("⚠️ Couldn't reload the recipe.")
        return

    push_recipe_to_display(row)
    await update.message.reply_text(f"{'⭐' * rating} Rating updated.")


async def cmd_search(update: Update, context) -> None:
    """Full-text search the saved recipe library; tap a result to push it."""
    if not _is_allowed(update.effective_user.id):
        return

    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text("Usage: `/search <query>`", parse_mode="Markdown")
        return

    results = library.search(query, limit=5)
    if not results:
        await update.message.reply_text(f"No saved recipes match '{query}'.")
        return

    buttons = []
    for r in results:
        stars = "★" * r["rating"] if r["rating"] else "·"
        saved_date = (
            datetime.fromtimestamp(r["saved_at"]).strftime("%Y-%m-%d")
            if r["saved_at"] else "—"
        )
        label = f"{stars} {r['title']} · {saved_date}"
        # Telegram inline button labels truncate around 64 bytes; keep ASCII-safe headroom.
        if len(label) > 60:
            label = label[:57] + "..."
        buttons.append([InlineKeyboardButton(label, callback_data=f"push:{r['id']}")])

    await update.message.reply_text(
        f"Top matches for '{query}':",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def on_push_button(update: Update, context) -> None:
    """User tapped a /search result — render and push that recipe."""
    query = update.callback_query
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

    push_recipe_to_display(row)
    stars = ("⭐" * row["rating"]) if row["rating"] else ""
    await query.answer(f"Pushed: {row['title']} {stars}".strip())


async def on_photo(update: Update, context) -> None:
    """Handle photo messages — resize and send to display."""
    if not _is_allowed(update.effective_user.id):
        return

    log.info("Photo received from user %s", update.effective_user.id)
    msg = await update.message.reply_text("📸 Processing image...")

    # Get the highest resolution photo
    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()

    img = process_photo(bytes(image_bytes))
    display_state.set_image(img, content_type="photo")

    await msg.edit_text("✅ Photo sent to display!")


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
    """Fetch a recipe URL, render all pages, push to display, and reply."""
    recipe = await process_recipe_url(url)
    if recipe is None:
        log.warning("Failed to parse recipe from URL: %s", url)
        await msg.edit_text("❌ Couldn't parse a recipe from that URL.\nTry sending a screenshot instead.")
        return
    await _present_recipe(url, recipe, msg)


async def _present_recipe(url: str, recipe: dict, msg) -> None:
    """Push a parsed recipe to the display.

    If `url` matches an already-saved row, restore its rating + comments
    and skip the Save prompt. Otherwise stash in the pending map and offer
    a 💾 Save button — the DB row is only created when the user rates.
    """
    existing = library.find_by_url(url)
    if existing is not None:
        push_recipe_to_display(existing)
        rating_badge = ("⭐" * existing["rating"]) if existing["rating"] else "saved"
        total = display_state.get()["total_pages"]
        reply = f"✅ *{existing['title']}* ({rating_badge})\nSent to display!"
        if total > 1:
            reply += f"\n📄 {total} pages"
        await msg.edit_text(reply, parse_mode="Markdown")
        return

    display_state.set_recipe(recipe, comments=[], recipe_id=None, url=url)
    total_pages = display_state.get()["total_pages"]

    token = _stash_pending(url, recipe)
    reply = f"✅ *{recipe['title']}*\nSent to display!"
    if total_pages > 1:
        reply += f"\n📄 {total_pages} pages"
    await msg.edit_text(
        reply,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("💾 Save", callback_data=f"save:{token}")
        ]]),
    )


def _synthetic_jsonld_url(recipe: dict) -> str:
    """Stable surrogate URL for JSON-LD recipes without their own canonical URL.

    Hashing title + ingredients + instructions means re-uploading the same
    LLM output collides on the library's UNIQUE(url) and dedupes cleanly.
    """
    payload = json.dumps(
        {
            "title": recipe.get("title", ""),
            "ingredients": recipe.get("ingredients", []),
            "instructions": recipe.get("instructions", []),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    return f"jsonld:{digest}"


async def on_document(update: Update, context) -> None:
    """Handle .json uploads — schema.org Recipe JSON-LD ingest."""
    if not _is_allowed(update.effective_user.id):
        return

    doc = update.message.document
    log.info(
        "Document received from user %s: name=%s mime=%s size=%s",
        update.effective_user.id, doc.file_name, doc.mime_type, doc.file_size,
    )

    msg = await update.message.reply_text("🧾 Reading JSON...")

    if doc.file_size and doc.file_size > _JSON_MAX_BYTES:
        await msg.edit_text(
            f"❌ JSON file too large ({doc.file_size} bytes; limit "
            f"{_JSON_MAX_BYTES}).",
        )
        return

    file = await doc.get_file()
    raw = await file.download_as_bytearray()
    try:
        data = json.loads(bytes(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        log.warning("Failed to parse uploaded JSON: %s", e)
        await msg.edit_text("❌ Couldn't parse the file as JSON.")
        return

    parsed = parse_recipe_jsonld(data)
    if parsed is None:
        await msg.edit_text(
            "❌ No schema.org Recipe found.\n"
            "Expecting an object with `@type: \"Recipe\"`, plus at least "
            "`name` and one of `recipeIngredient` / `recipeInstructions`.",
            parse_mode="Markdown",
        )
        return

    recipe, source_url = parsed
    url = source_url or _synthetic_jsonld_url(recipe)
    log.info("JSON-LD recipe ingested: title=%r url=%s", recipe.get("title"), url)
    await _present_recipe(url, recipe, msg)


def push_recipe_to_display(row: dict) -> None:
    """Render the recipe in `row` with its current comments + rating and push to the panel."""
    comments = [c["body"] for c in library.get_comments(row["id"])]
    display_state.set_recipe(
        row["recipe"],
        comments=comments,
        rating=row.get("rating"),
        recipe_id=row["id"],
        url=row["url"],
    )


async def on_save_button(update: Update, context) -> None:
    """User tapped 💾 Save — show 1-5 star rating buttons."""
    query = update.callback_query
    _, token = query.data.split(":")

    if token not in _pending:
        await query.answer("Session expired — repush the URL to save.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    await query.answer("Rate 1–5 stars to confirm")
    await query.edit_message_reply_markup(
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"{n}⭐", callback_data=f"rate:{token}:{n}")
            for n in range(1, 6)
        ]])
    )


async def on_rate_button(update: Update, context) -> None:
    """User tapped a star — persist recipe + rating, confirm in chat."""
    query = update.callback_query
    _, token, rating_str = query.data.split(":")
    rating = int(rating_str)

    pending = _pending.pop(token, None)
    if pending is None:
        await query.answer("Session expired — repush the URL to save.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    url, recipe = pending
    recipe_id = library.upsert_recipe(url, recipe)
    library.mark_saved(recipe_id, rating)

    # If this recipe is still on the display, re-render so the rating
    # stars appear on the panel. Compare by URL — title alone can collide.
    state = display_state.get()
    if state["type"] == "recipe" and state["url"] == url:
        push_recipe_to_display(library.get_recipe(recipe_id))

    stars = "⭐" * rating
    await query.answer(f"{stars} Saved")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if query.message is not None:
        await query.message.reply_text(f"💾 Saved {stars}")
