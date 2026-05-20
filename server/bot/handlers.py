"""Telegram bot handlers for ePepper."""

import html
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

from config import TELEGRAM_BOT_TOKEN, ALLOWED_USERS, WEB_URL
import backup
import display_state
import library
from display_push import push_recipe_to_display
from processing.images import process_photo
from processing.jsonld import parse_recipe_jsonld, resolve_url
from processing.recipes import process_recipe_url
from status_helpers import battery_pct, humanize_ago, rssi_quality, source_name

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
        f"🪫 ePepper battery is low: {battery_pct(battery_mv)}% "
        f"({battery_mv / 1000:.2f} V) — charge soon."
    )
    for uid in ALLOWED_USERS:
        try:
            await _bot_app.bot.send_message(chat_id=uid, text=text)
            log.info("Low-battery alert sent to user %s (%dmV)", uid, battery_mv)
        except Exception:
            log.exception("Failed to send low-battery alert to user %s", uid)


async def notify_stale_heartbeat(hours_since: int) -> None:
    """Push a one-shot warning when the device hasn't checked in for ≥25 h.

    Called by the scheduler's heartbeat_loop the first time the staleness
    threshold is crossed (display_state owns the alerted flag). Re-armed
    automatically on the next successful /device/status POST.
    """
    if _bot_app is None:
        log.warning("notify_stale_heartbeat: bot not yet initialised")
        return
    if not ALLOWED_USERS:
        log.warning("notify_stale_heartbeat: no ALLOWED_USERS configured, skipping alert")
        return
    text = (
        f"⚠️ ePepper hasn't checked in for {hours_since}h — "
        f"battery may be flat or Wi-Fi down."
    )
    for uid in ALLOWED_USERS:
        try:
            await _bot_app.bot.send_message(chat_id=uid, text=text)
            log.info("Stale-heartbeat alert sent to user %s (%dh)", uid, hours_since)
        except Exception:
            log.exception("Failed to send stale-heartbeat alert to user %s", uid)


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
    backup.set_bot(app.bot)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("recipe", cmd_recipe))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("comment", cmd_comment))
    app.add_handler(CommandHandler("rate", cmd_rate))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("surprise", cmd_surprise))
    app.add_handler(CommandHandler("prompt_screenshot", cmd_prompt_screenshot))
    app.add_handler(CommandHandler("prompt_url", cmd_prompt_url))
    app.add_handler(CommandHandler("prompt_croqumenus", cmd_prompt_croqumenus))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(
        filters.Document.FileExtension("json") | filters.Document.MimeType("application/json"),
        on_document,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_save_button, pattern=r"^save:"))
    app.add_handler(CallbackQueryHandler(on_rate_button, pattern=r"^rate:"))
    app.add_handler(CallbackQueryHandler(on_push_button, pattern=r"^push:"))
    # Catch-all for unknown /commands — must register after every named
    # CommandHandler so known commands match first.
    app.add_handler(MessageHandler(filters.COMMAND, on_unknown_command))

    return app


async def on_unknown_command(update: Update, context) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    await update.message.reply_text("Unknown command — try /help.")


def _is_allowed(user_id: int) -> bool:
    """Check if user is allowed (empty list = allow all)."""
    if not ALLOWED_USERS:
        return True
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
            "(same API_KEY logs you in) — sort, filter, and browse the full library."
        )
    return (
        "🌐 <b>Web app:</b> open <code>/app/</code> on your server "
        "(same API_KEY logs you in) — sort, filter, and browse the full library."
    )


_START_TEXT = (
    "🫑 <b>ePepper — your kitchen recipe display</b>\n\n"
    "<b>Send me:</b>\n"
    "• A photo of a recipe\n"
    "• A recipe URL (just paste the link)\n"
    "• A .json file with a schema.org Recipe (for OCR / unsupported sites)\n\n"
    "Tap 💾 <b>Save</b> under a pushed recipe to keep it in your library "
    "(rate 1–5 stars to confirm). Use the device's <b>physical buttons</b> "
    "to cycle between recipe pages.\n\n"
    "💡 <b>Tip:</b> for unsupported sites, /prompt_url or /prompt_screenshot "
    "give you an LLM prompt that produces a JSON-LD file you can upload here.\n\n"
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


_HELP_TEXT = (
    "🫑 <b>ePepper — commands</b>\n\n"
    "<b>➕ Add a recipe</b>\n"
    "Send a photo, a URL, or a .json (schema.org Recipe).\n"
    "/recipe &lt;url&gt; — force-parse a specific URL\n"
    "/prompt_screenshot — LLM prompt to OCR a photo → JSON-LD\n"
    "/prompt_url &lt;url&gt; — LLM prompt to fetch a URL → JSON-LD\n"
    "/prompt_croqumenus &lt;url&gt; — JSON-LD prompt tuned for croqumenus.ch / meintiptopf.ch\n\n"
    "<b>📚 Library</b>\n"
    "Tap 💾 Save under a pushed recipe (rate 1–5 to confirm).\n"
    "/search &lt;query&gt; — find a saved recipe\n"
    "/surprise — push a random saved recipe to the display\n"
    "/rate &lt;1-5&gt; — update rating of the displayed recipe\n"
    "/comment &lt;text&gt; — add a note to the displayed recipe\n\n"
    "<b>📺 Display</b>\n"
    "The device's physical buttons cycle pages.\n"
    "/clear — clear the display\n\n"
    "<b>ℹ️ Info</b>\n"
    "/status — device status\n"
    "/help — this message\n\n"
    "{web_line}"
)


async def cmd_help(update: Update, context) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        _HELP_TEXT.format(web_line=_web_app_line()),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# Shared body of the LLM prompts. Tracks parse_recipe_jsonld's expected fields
# (server/processing/jsonld.py) — keep in sync when that mapping changes.
_PROMPT_JSON_TEMPLATE = """{
  "@context": "https://schema.org/",
  "@type": "Recipe",
  "name": "<recipe title>",
  "inLanguage": "<en|de|fr|it>",
  "totalTime": "PT45M",
  "recipeYield": "<servings, e.g. 4 servings>",
  "recipeIngredient": [
    "<ingredient 1, with quantity and unit>",
    "<ingredient 2>"
  ],
  "recipeInstructions": [
    { "@type": "HowToStep", "text": "<step 1>" },
    { "@type": "HowToStep", "text": "<step 2>" }
  ]
}"""

_PROMPT_RULES = """Rules:
- Don't invent or extrapolate any field — omit it if unclear.
- inLanguage: pick one of en/de/fr/it matching the recipe text.
- totalTime: ISO 8601 (PT45M, PT1H30M). Omit if the recipe doesn't say.
- recipeYield: numeric where possible ("4 servings", "12 cookies").
- Preserve ingredient quantities and order exactly as written.
- If the recipe has section headings (e.g. "Sauce", "Dough"), wrap their
  steps in HowToSection:
    { "@type": "HowToSection", "name": "Sauce",
      "itemListElement": [ { "@type": "HowToStep", "text": "..." } ] }
- Output ONE single JSON object inside ONE code block — no surrounding prose,
  no markdown commentary.
- Also offer the JSON as a downloadable file named `recipe.json` so it can be
  attached directly without copy-pasting."""


# Screenshot-specific URL rule: ask the LLM to identify the cookbook /
# magazine / source from the photo and put it into the cookbook:// URL,
# with a kebab-case title slug for the path. The server preserves these
# URLs as-is; on the panel + status surfaces the netloc renders as
# "from Nos-recettes-preferees" (no link — cookbook:// isn't browseable).
# If the LLM truly can't guess the source, it falls back to "cookbook"
# as a generic netloc.
_PROMPT_SCREENSHOT_URL_RULE = (
    "- `url`: build it as `cookbook://<source-slug>/<title-slug>`. The "
    "source-slug is a kebab-case identifier you infer from any visible "
    "branding in the photo — the cookbook title on the cover/spine, a "
    "magazine name, a restaurant — lowercase ASCII, dashes for spaces. "
    "If you really can't tell, use `cookbook` as the source-slug. The "
    "title-slug is the recipe title in the same kebab-case form. "
    "Example: `cookbook://nos-recettes-preferees/crepes-bretonnes`."
)


def _build_screenshot_prompt() -> str:
    template = _PROMPT_JSON_TEMPLATE.replace(
        '"@type": "Recipe",',
        '"@type": "Recipe",\n  "url": "cookbook://<source-slug>/<title-slug>",',
    )
    return (
        "I'm attaching a photo of a recipe. Convert it to schema.org Recipe "
        "JSON-LD with this exact shape:\n\n"
        f"{template}\n\n"
        f"{_PROMPT_RULES}\n"
        f"{_PROMPT_SCREENSHOT_URL_RULE}"
    )


def _build_url_prompt(url: str | None) -> str:
    if url:
        intro = (
            f"Fetch this URL and convert the recipe to schema.org Recipe "
            f"JSON-LD:\n  {url}\n\n"
            "Output exactly this shape (include the source URL so the "
            "ePepper library can dedupe):\n\n"
        )
        template = _PROMPT_JSON_TEMPLATE.replace(
            '"@type": "Recipe",',
            f'"@type": "Recipe",\n  "url": "{url}",',
        )
    else:
        intro = (
            "Fetch the recipe webpage at the URL I'm about to share and "
            "convert it to schema.org Recipe JSON-LD with this exact shape "
            "(include the source URL in the JSON so the ePepper library "
            "can dedupe):\n\n"
        )
        template = _PROMPT_JSON_TEMPLATE.replace(
            '"@type": "Recipe",',
            '"@type": "Recipe",\n  "url": "<source URL>",',
        )
    return f"{intro}{template}\n\n{_PROMPT_RULES}"


def _build_croqumenus_prompt(url: str | None) -> str:
    """Site-specific prompt for croqumenus.ch / meintiptopf.ch.

    These two sites share a backend (api.meintiptopf.ch) and embed the recipe
    as a `recipeDetails` JSON blob in the page HTML. A generic LLM that scrapes
    the rendered text mangles French grammar (missing partitive `de`, ingredient
    qualifiers injected mid-sentence). Pointing the LLM at the structured blob
    plus the transformer quirks gives clean output without an in-tree scraper.
    See [[project_croqumenus_import_flow]] memory for the rationale.
    """
    url_line = url if url else "<paste the croqumenus.ch URL here>"
    template_url = url if url else "<source URL>"
    template = _PROMPT_JSON_TEMPLATE.replace(
        '"@type": "Recipe",',
        f'"@type": "Recipe",\n  "url": "{template_url}",',
    )
    return (
        f"Fetch this croqumenus.ch (or meintiptopf.ch) URL and convert it to "
        f"schema.org Recipe JSON-LD:\n  {url_line}\n\n"
        "These two sites embed the recipe as a JSON blob in the HTML — DO NOT "
        "scrape the rendered text, use the blob (the rendered prose has "
        "ambiguous quantities and the LLM-flattening produces broken French).\n\n"
        "1. Fetch with `curl -sL -A \"Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36\"` — a default User-Agent returns a stripped page.\n"
        "2. Locate `\"recipeDetails\":{...}` in the HTML and extract the object "
        "by brace-matching (response is ~1.5 MB, with strings escaped).\n"
        "3. Field map:\n"
        "   - `rezept_titel` → `name`\n"
        "   - `zubereitungszeit` (minutes, already the total) → `totalTime` "
        "as ISO 8601 (e.g. 75 → PT1H15M)\n"
        "   - `anzahl_portionen_big` → `recipeYield` as `\"N portions\"`; "
        "fallback to HTML-stripped `portionsgroesse` (e.g. `plaque de cuisson`)\n"
        "   - `inLanguage`: `\"fr\"` for croqumenus.ch, `\"de\"` for meintiptopf.ch\n"
        "4. Iterate `steps_portion_big`. Each entry has `step_titel`, "
        "`step_beschreibung` (HTML `<ul><li>...</li></ul>` — strip `<a "
        "class=\"glossary-term\">` keeping inner text, treat each `<li>` as one "
        "action phrase), and `step_zutaten` (ingredients used in that step).\n"
        "5. Build the master `recipeIngredient` list by concatenating all "
        "`step_zutaten` IN ORDER, with these filters:\n"
        "   - SKIP entries whose `zutat_namen` ends in "
        "`préparé/préparée/préparés/préparées` — back-references to a prior step.\n"
        "   - An entry with `zutat_id: 0` and only `step_zutat_freitext` "
        "populated is an annotation — fold it as ` (note)` onto the PREVIOUS "
        "ingredient.\n"
        "   - Format each ingredient:\n"
        "     • menge > 0: `{menge} {abkuerzung} {name}` (e.g. "
        "`400 g pommes de terre`)\n"
        "     • menge = 0 AND abkuerzung set: `{abkuerzung} {name}` (e.g. "
        "`un peu poivre`)\n"
        "     • else: just `{name}` (e.g. `eau`)\n"
        "6. For each step's text, write a natural French sentence (NOT raw "
        "bullets):\n"
        "   - Capitalize the first action verb; integrate the step's "
        "ingredients into that first phrase, joined with `, ` and a final ` et `"
        " (e.g. `Verser dans la poêle 2 cs huile, chauffer à feu très vif`).\n"
        "   - SKIP \"ghost\" ingredients from the step's subject list — menge=0 "
        "AND no unit AND no freitext AND not a back-ref. They're already "
        "implied by the action (e.g. don't repeat `eau` when the action says "
        "`Remplir la casserole d'eau`). They DO stay in the master "
        "`recipeIngredient` list.\n"
        "   - Back-refs (`préparé...`) DO appear in the step's subject — they "
        "tell the reader what to re-add.\n"
        "   - Subsequent bullets become separate sentences, each capitalized, "
        "joined with `. `.\n"
        "   - Prepend `[step_titel] ` if non-empty.\n\n"
        "Output exactly this shape:\n\n"
        f"{template}\n\n"
        "Rules:\n"
        "- Don't invent or extrapolate any field — omit if not in the source.\n"
        "- Preserve ingredient quantities and order exactly.\n"
        "- Output ONE single JSON object inside ONE code block — no "
        "surrounding prose, no commentary.\n"
        "- Also offer the JSON as a downloadable file named `recipe.json`."
    )


_PROMPT_OUTRO = (
    "Then send me the <code>recipe.json</code> file — either the one the "
    "assistant offers as a download, or save the code block to a file "
    "yourself. I'll parse it and push it to the display."
)


async def cmd_prompt_screenshot(update: Update, context) -> None:
    """Emit an LLM-ready prompt for OCR-ing a recipe photo into JSON-LD."""
    if not _is_allowed(update.effective_user.id):
        return
    prompt = _build_screenshot_prompt()
    await update.message.reply_text(
        "Tap-and-hold to copy this prompt, then paste it into your LLM "
        "(Claude, ChatGPT, etc.) together with a screenshot of the recipe:\n\n"
        f"<pre>{html.escape(prompt)}</pre>\n\n"
        f"{_PROMPT_OUTRO}",
        parse_mode="HTML",
    )


async def cmd_prompt_url(update: Update, context) -> None:
    """Emit an LLM-ready prompt for fetching a URL and converting it to JSON-LD."""
    if not _is_allowed(update.effective_user.id):
        return
    url = context.args[0].strip() if context.args else None
    prompt = _build_url_prompt(url)
    intro_extra = (
        ""
        if url
        else "Pass it to an LLM that can browse (Claude with web tools, "
        "ChatGPT with browsing, Perplexity, …) — or use it as a template "
        "and paste the URL inline.\n\n"
    )
    await update.message.reply_text(
        f"{intro_extra}Tap-and-hold to copy:\n\n"
        f"<pre>{html.escape(prompt)}</pre>\n\n"
        f"{_PROMPT_OUTRO}",
        parse_mode="HTML",
    )


async def cmd_prompt_croqumenus(update: Update, context) -> None:
    """Emit a croqumenus.ch / meintiptopf.ch-specific JSON-LD prompt.

    Exploits the embedded `recipeDetails` blob so the LLM doesn't have to
    re-derive ingredient/step alignment from rendered prose (which produces
    broken French — see [[project_croqumenus_import_flow]]).
    """
    if not _is_allowed(update.effective_user.id):
        return
    url = context.args[0].strip() if context.args else None
    prompt = _build_croqumenus_prompt(url)
    intro_extra = (
        ""
        if url
        else "Best run in an LLM with code execution (Claude Code, ChatGPT "
        "with code interpreter) so it can curl + brace-match the JSON blob.\n\n"
    )
    await update.message.reply_text(
        f"{intro_extra}Tap-and-hold to copy:\n\n"
        f"<pre>{html.escape(prompt)}</pre>\n\n"
        f"{_PROMPT_OUTRO}",
        parse_mode="HTML",
    )


async def cmd_recipe(update: Update, context) -> None:
    """Parse a recipe URL and display it."""
    if not _is_allowed(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: <code>/recipe &lt;url&gt;</code>", parse_mode="HTML")
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

    # Library section
    library_lines = [f"<b>📚 Library</b>", f"{library.count_saved()} saved recipes"]
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
            if int(time.time()) - device["last_seen"] > display_state.STALE_HEARTBEAT_S
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

    await update.message.reply_text("\n\n".join(sections), parse_mode="HTML")


async def cmd_comment(update: Update, context) -> None:
    """Add a comment to the currently-displayed *saved* recipe."""
    if not _is_allowed(update.effective_user.id):
        return

    state = display_state.get()
    recipe_id = state.get("recipe_id")

    if state["type"] != "recipe" or recipe_id is None:
        await update.message.reply_text(
            "Save the recipe first (tap 💾 <b>Save</b> under the push message), "
            "then add notes with /comment.",
            parse_mode="HTML",
        )
        return

    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await update.message.reply_text(
            "Usage: <code>/comment &lt;your note&gt;</code>", parse_mode="HTML"
        )
        return

    library.add_comment(recipe_id, text)
    log.info("Comment added to recipe %d (%d chars)", recipe_id, len(text))

    row = library.get_recipe(recipe_id)
    if row is None:
        await update.message.reply_text("⚠️ Couldn't reload the recipe.")
        return

    push_recipe_to_display(row)
    await update.message.reply_text("📝 Note added.")


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
        await update.message.reply_text("Usage: <code>/rate &lt;1-5&gt;</code>", parse_mode="HTML")
        return

    if not library.mark_saved(recipe_id, rating):
        # Soft-deleted between push and /rate, or the row vanished.
        await update.message.reply_text(
            "⚠️ That recipe is gone — push a saved one to the display first."
        )
        return
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
        await update.message.reply_text("Usage: <code>/search &lt;query&gt;</code>", parse_mode="HTML")
        return

    results = library.search(query, limit=5)
    if not results:
        await update.message.reply_text(
            f"No saved recipes match '{query}'. Try a shorter or different term."
        )
        return

    # Render results as a readable numbered list above the keyboard, then
    # let the user pick by number — avoids cramming title + stars + date
    # into the 64-byte inline button label.
    lines = [f"🔍 <b>Matches for \"{html.escape(query)}\"</b>", ""]
    buttons = []
    for i, r in enumerate(results, start=1):
        stars = ("⭐" * r["rating"]) if r["rating"] else ""
        title = html.escape(r["title"])
        saved_date = (
            datetime.fromtimestamp(r["saved_at"]).strftime("%Y-%m-%d")
            if r["saved_at"] else "—"
        )
        lines.append(f"<b>{i}.</b> {title} {stars}".rstrip())
        lines.append(f"<i>   saved {saved_date}</i>")
        lines.append("")
        buttons.append(InlineKeyboardButton(str(i), callback_data=f"push:{r['id']}"))

    await update.message.reply_text(
        "\n".join(lines).rstrip(),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([buttons]),
    )


async def cmd_surprise(update: Update, context) -> None:
    """Pick a random saved recipe and push it to the display."""
    if not _is_allowed(update.effective_user.id):
        return
    row = library.random_recipe()
    if row is None:
        await update.message.reply_text(
            "Your library is empty — save a recipe first."
        )
        return
    push_recipe_to_display(row)
    total = display_state.get()["total_pages"]
    log.info("Surprise push: id=%d title=%r", row["id"], row["title"])
    await update.message.reply_text(
        "🎲 " + _format_push_reply(row["title"], row.get("rating"), total),
        parse_mode="HTML",
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

    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        image_bytes = await file.download_as_bytearray()

        img = process_photo(bytes(image_bytes))
        display_state.set_image(img, content_type="photo")
    except Exception:
        # process_photo can raise on HEIC / corrupt JPEG / other Pillow
        # weirdness; without this the "Processing image..." sticks forever.
        log.exception("Photo processing failed")
        await msg.edit_text(
            "❌ Couldn't process the photo.\n"
            "Try /prompt_screenshot — paste the prompt into an LLM with the "
            "photo, then send me the recipe.json it produces."
        )
        return

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
        await msg.edit_text(
            "❌ Couldn't parse a recipe from that URL.\n"
            "Try /prompt_url with the URL — paste the prompt into an LLM, "
            "then send me the recipe.json it returns. Or send a screenshot "
            "and use /prompt_screenshot."
        )
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
        total = display_state.get()["total_pages"]
        await msg.edit_text(
            _format_push_reply(existing["title"], existing["rating"], total),
            parse_mode="HTML",
        )
        return

    display_state.set_recipe(recipe, comments=[], recipe_id=None, url=url)
    total_pages = display_state.get()["total_pages"]

    token = _stash_pending(url, recipe)
    await msg.edit_text(
        _format_push_reply(recipe["title"], rating=None, total_pages=total_pages),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("💾 Save", callback_data=f"save:{token}")
        ]]),
    )


def _format_push_reply(title: str, rating: int | None, total_pages: int) -> str:
    """Two-line confirmation for a pushed recipe (HTML, matches /status style)."""
    meta_parts = []
    if rating:
        meta_parts.append("⭐" * rating)
    if total_pages > 1:
        meta_parts.append(f"📄 {total_pages} pages")
    body = f"✅ <b>{html.escape(title)}</b>"
    if meta_parts:
        body += "\n" + " · ".join(meta_parts)
    return body


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
            f"❌ JSON file too large ({doc.file_size // 1024} KB; limit "
            f"{_JSON_MAX_BYTES // 1024} KB)."
        )
        return

    file = await doc.get_file()
    raw = await file.download_as_bytearray()
    try:
        data = json.loads(bytes(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        log.warning("Failed to parse uploaded JSON: %s", e)
        await msg.edit_text(
            "❌ Couldn't parse the file as JSON.\n"
            "If the LLM output isn't valid JSON, re-run /prompt_url or "
            "/prompt_screenshot and try again."
        )
        return

    parsed = parse_recipe_jsonld(data)
    if parsed is None:
        await msg.edit_text(
            "❌ No schema.org Recipe found.\n"
            "Expecting an object with <code>@type: \"Recipe\"</code>, plus "
            "at least <code>name</code> and one of "
            "<code>recipeIngredient</code> / <code>recipeInstructions</code>.\n"
            "Re-run /prompt_url or /prompt_screenshot — that prompt produces "
            "the right shape.",
            parse_mode="HTML",
        )
        return

    recipe, source_url = parsed
    url = resolve_url(source_url, recipe)
    log.info("JSON-LD recipe ingested: title=%r url=%s", recipe.get("title"), url)
    await _present_recipe(url, recipe, msg)


async def on_save_button(update: Update, context) -> None:
    """User tapped 💾 Save — show 1-5 star rating buttons."""
    query = update.callback_query
    try:
        _, token = query.data.split(":", 1)
    except ValueError:
        log.warning("on_save_button: malformed callback data %r", query.data)
        await query.answer("Bad callback.", show_alert=True)
        return

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
    try:
        _, token, rating_str = query.data.split(":", 2)
        rating = int(rating_str)
    except (ValueError, AttributeError):
        log.warning("on_rate_button: malformed callback data %r", query.data)
        await query.answer("Bad callback.", show_alert=True)
        return
    if not 1 <= rating <= 5:
        await query.answer("Bad rating.", show_alert=True)
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
