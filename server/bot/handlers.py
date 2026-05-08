"""Telegram bot handlers for ePepper."""

import logging

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
from processing.images import process_photo
from processing.recipes import process_recipe_url
from rendering.layout import render_recipe

log = logging.getLogger(__name__)


def create_bot() -> Application:
    """Create and configure the Telegram bot application."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("recipe", cmd_recipe))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_page_button, pattern=r"^page:"))

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
        "/clear — clear the display\n"
        "/status — device info\n"
        "/help — this message",
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

    recipe = await process_recipe_url(url)
    if recipe is None:
        await msg.edit_text("❌ Couldn't parse a recipe from that URL.")
        return

    img, total_pages = render_recipe(recipe, page=1)

    # Store all pages
    pages = {1: img}
    for p in range(2, total_pages + 1):
        page_img, _ = render_recipe(recipe, page=p)
        pages[p] = page_img

    display_state.set_recipe_pages(pages, title=recipe.get("title", ""))

    reply = f"✅ *{recipe['title']}*\nSent to display!"
    if total_pages > 1:
        reply += f"\n📄 {total_pages} pages"
        await msg.edit_text(reply, parse_mode="Markdown", reply_markup=_page_keyboard(1, total_pages))
    else:
        await msg.edit_text(reply, parse_mode="Markdown")


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
        import time
        ago = int(time.time() - device["last_seen"])
        lines.append(f"Last seen: {ago}s ago")
    else:
        lines.append("Last seen: never")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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

    # Try to parse as recipe
    log.info("Received URL from user %s: %s", update.effective_user.id, text)
    msg = await update.message.reply_text("🔍 Fetching recipe...")

    recipe = await process_recipe_url(text)
    if recipe is None:
        log.warning("Failed to parse recipe from URL: %s", text)
        await msg.edit_text("❌ Couldn't parse a recipe from that URL.\nTry sending a screenshot instead.")
        return

    img, total_pages = render_recipe(recipe, page=1)

    pages = {1: img}
    for p in range(2, total_pages + 1):
        page_img, _ = render_recipe(recipe, page=p)
        pages[p] = page_img

    display_state.set_recipe_pages(pages, title=recipe.get("title", ""))

    reply = f"✅ *{recipe['title']}*\nSent to display!"
    if total_pages > 1:
        reply += f"\n📄 {total_pages} pages"
        await msg.edit_text(reply, parse_mode="Markdown", reply_markup=_page_keyboard(1, total_pages))
    else:
        await msg.edit_text(reply, parse_mode="Markdown")


async def on_page_button(update: Update, context) -> None:
    """Handle page navigation buttons."""
    query = update.callback_query

    _, page_str = query.data.split(":")
    if page_str == "noop":
        await query.answer()
        return

    page = int(page_str)
    if display_state.set_page(page):
        state = display_state.get()
        await query.answer(f"Page {page}/{state['total_pages']}")
        await query.edit_message_reply_markup(
            reply_markup=_page_keyboard(page, state["total_pages"])
        )
    else:
        await query.answer("Invalid page")


def _page_keyboard(current: int, total: int) -> InlineKeyboardMarkup:
    """Build page navigation inline keyboard."""
    buttons = []
    if current > 1:
        buttons.append(InlineKeyboardButton("◀️ Prev", callback_data=f"page:{current - 1}"))
    buttons.append(InlineKeyboardButton(f"📄 {current}/{total}", callback_data="page:noop"))
    if current < total:
        buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"page:{current + 1}"))
    return InlineKeyboardMarkup([buttons])
