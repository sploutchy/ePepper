"""Telegram bot handlers for ePepper."""

import logging
import time
import uuid
from collections import OrderedDict
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
from processing.recipes import process_recipe_url
from rendering.layout import render_recipe

log = logging.getLogger(__name__)


# Pending unsaved recipes, keyed by a short token embedded in the Save
# button's callback_data. The map only holds parsed recipes the user has
# not yet rated; once a star is tapped (or 32 newer pushes have arrived)
# the entry is removed.
_PENDING_MAX = 32
_pending: "OrderedDict[str, Tuple[str, dict]]" = OrderedDict()


def _stash_pending(url: str, recipe: dict) -> str:
    token = uuid.uuid4().hex[:8]
    _pending[token] = (url, recipe)
    while len(_pending) > _PENDING_MAX:
        _pending.popitem(last=False)
    return token


def create_bot() -> Application:
    """Create and configure the Telegram bot application."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("recipe", cmd_recipe))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("comment", cmd_comment))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_save_button, pattern=r"^save:"))
    app.add_handler(CallbackQueryHandler(on_rate_button, pattern=r"^rate:"))

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
        "• `/recipe <url>` to force recipe parsing\n\n"
        "Commands:\n"
        "/comment <text> — add a note to a saved recipe (must save first)\n"
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

    # Battery percentage estimate (Li-Ion: 3.0V empty, 4.2V full)
    batt_pct = ""
    if device["battery_mv"] > 0:
        pct = max(0, min(100, int((device["battery_mv"] - 3000) / 12)))
        batt_pct = f"🔋 {pct}% ({device['battery_mv']}mV)"

    lines = [
        "📊 *ePepper Status*",
        "",
        f"Display: {state['type']}",
    ]
    if state["title"]:
        lines.append(f"Recipe: {state['title']}")
    if state["total_pages"] > 1:
        lines.append(f"Page: {state['page']}/{state['total_pages']}")
    if batt_pct:
        lines.append(batt_pct)
    if device["rssi"]:
        lines.append(f"📶 WiFi: {device['rssi']} dBm")
    if device["last_seen"]:
        ago = int(time.time() - device["last_seen"])
        lines.append(f"Last seen: {ago}s ago")
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

    _push_recipe_to_display(row)
    total = display_state.get()["total_pages"]
    await update.message.reply_text(
        f"📝 Note added — recipe now spans {total} page{'s' if total != 1 else ''}."
    )


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

    # If this URL is already in the library, treat it as a re-display of a
    # saved recipe (carry over comments + rating, no Save button).
    existing = library.find_by_url(url)
    if existing is not None:
        _push_recipe_to_display(existing)
        rating_badge = ("⭐" * existing["rating"]) if existing["rating"] else "saved"
        total = display_state.get()["total_pages"]
        reply = f"✅ *{existing['title']}* ({rating_badge})\nSent to display!"
        if total > 1:
            reply += f"\n📄 {total} pages"
        await msg.edit_text(reply, parse_mode="Markdown")
        return

    # Brand-new recipe: render without notes, offer a Save button. Don't
    # touch the DB until the user rates.
    pages = _render_all_pages(recipe, comments=[])
    total_pages = len(pages)
    display_state.set_recipe_pages(
        pages,
        title=recipe.get("title", ""),
        lang=recipe.get("lang", "en"),
        recipe_id=None,
        url=url,
    )

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


def _render_all_pages(recipe: dict, comments: list[str], rating: int | None = None) -> dict:
    """Render every page of a recipe (with optional notes/rating) into a {page: Image} dict."""
    first_img, total_pages = render_recipe(recipe, page=1, comments=comments, rating=rating)
    pages = {1: first_img}
    for p in range(2, total_pages + 1):
        page_img, _ = render_recipe(recipe, page=p, comments=comments, rating=rating)
        pages[p] = page_img
    return pages


def _push_recipe_to_display(row: dict) -> None:
    """Render the recipe in `row` with its current comments + rating and push to the panel."""
    comments = [c["body"] for c in library.get_comments(row["id"])]
    pages = _render_all_pages(row["recipe"], comments, rating=row.get("rating"))
    display_state.set_recipe_pages(
        pages,
        title=row["title"],
        lang=row["lang"],
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
        _push_recipe_to_display(library.get_recipe(recipe_id))

    stars = "⭐" * rating
    await query.answer(f"{stars} Saved")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if query.message is not None:
        await query.message.reply_text(f"💾 Saved {stars}")
