"""Telegram bot handlers for ePepper."""

import html
import json
import logging
import time
import uuid
from collections import OrderedDict
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

from config import TELEGRAM_BOT_TOKEN, ALLOWED_USERS, WEB_URL
import backup
import display_state
import library
from display_push import push_recipe_to_display
from processing.jsonld import parse_recipe_jsonld, resolve_url
from processing.recipes import process_recipe_image, process_recipe_url
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

# Notes the user typed *before* saving a pending recipe — populated by
# /comment when the active display content isn't yet in the library.
# Tapping "Save & add note" drains both `_pending` and this map together.
_pending_notes: dict[str, str] = {}

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
# the menu shows them, so list daily-use commands first and the niche
# `/prompt_*` ones last. /start is omitted — it's a bootstrapping command.
_BOT_COMMANDS: list[tuple[str, str]] = [
    ("recipe", "Push a recipe URL to the display"),
    ("search", "Find a saved recipe"),
    ("surprise", "Pick a random saved recipe"),
    ("comment", "Add a note to the displayed recipe"),
    ("status", "Device + library status"),
    ("clear", "Clear the display"),
    ("help", "Show all commands"),
    ("prompt_url", "LLM prompt: URL → recipe.json"),
    ("prompt_screenshot", "LLM prompt: photo → recipe.json"),
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
    app.add_handler(CallbackQueryHandler(on_save_note_button, pattern=r"^save_note:"))
    app.add_handler(CallbackQueryHandler(on_push_button, pattern=r"^push:"))
    app.add_handler(CallbackQueryHandler(on_search_nav, pattern=r"^search:"))
    app.add_handler(CallbackQueryHandler(on_surprise_again, pattern=r"^surprise_again:"))
    app.add_handler(CallbackQueryHandler(on_surprise_push, pattern=r"^surprise_push:"))
    app.add_handler(CallbackQueryHandler(on_quick_action, pattern=r"^quick:"))
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
    "Tap 💾 <b>Save</b> under a pushed recipe to keep it in your library. "
    "Use the device's <b>physical buttons</b> to cycle between recipe "
    "pages.\n\n"
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


# Sectioned help — cleaner skim than one long wall of slash commands.
# Each section starts with a one-line "what this is for" then lists the
# commands that belong to it. The native /-menu (set via set_my_commands)
# covers the most-used ones, so /help is the deep reference.
_HELP_TEXT = (
    "🫑 <b>ePepper — help</b>\n\n"
    "<b>➕ Add a recipe</b>\n"
    "Just paste a URL, send a photo, or upload a schema.org Recipe .json.\n"
    "  /recipe &lt;url&gt; — force-parse a URL\n"
    "  /prompt_url, /prompt_screenshot — LLM prompts for unsupported sites\n"
    "  /prompt_croqumenus — JSON-LD prompt for croqumenus.ch / meintiptopf.ch\n\n"
    "<b>📚 Library</b>\n"
    "Tap 💾 Save under a push to keep a recipe.\n"
    "  /search &lt;query&gt; — find a saved recipe (paginated)\n"
    "  /surprise — pick a random saved recipe\n"
    "  /comment &lt;text&gt; — add a note to what's on screen\n\n"
    "<b>📺 Display</b>\n"
    "Physical buttons cycle pages.\n"
    "  /clear — clear the panel\n\n"
    "<b>ℹ️ Info</b>\n"
    "  /status — device + library snapshot\n"
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


def _build_status_text() -> str:
    """Render the /status sectioned snapshot as a single HTML string.

    Extracted from cmd_status so on_quick_action can call it from the
    /help quick-action button without smuggling Update / context through.
    """
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
            "Usage: <code>/comment &lt;your note&gt;</code>", parse_mode="HTML"
        )
        return

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
                )
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
    """Match the 'cooked N×, last DD.MM.YYYY' / 'never cooked' phrasing
    used across the web library cards so the bot's search results, surprise
    card, and status all describe the same recipe the same way."""
    if row.get("last_displayed_at"):
        last = datetime.fromtimestamp(row["last_displayed_at"]).strftime("%d.%m.%Y")
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
        push_buttons.append(InlineKeyboardButton(str(i), callback_data=f"push:{r['id']}"))

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    if push_buttons:
        keyboard_rows.append(push_buttons)

    nav_row: list[InlineKeyboardButton] = []
    if offset > 0 or has_more:
        token = _stash_search(query)
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
    """Full-text search the saved recipe library; tap a result to push it."""
    if not _is_allowed(update.effective_user.id):
        return

    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text(
            "Usage: <code>/search &lt;query&gt;</code>", parse_mode="HTML"
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
            "Your library is empty — save a recipe first."
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
        # Library has only the excluded recipe — fall back to picking it.
        if prev_id is not None:
            row = library.get_recipe(prev_id)
        if row is None:
            await query.answer("Library is empty.", show_alert=True)
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
    """User tapped a /search result — render and push that recipe."""
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
    await query.answer(f"Pushed: {row['title']}")


async def on_photo(update: Update, context) -> None:
    """Handle photo messages — OCR via LLM, then push the recipe to the display.

    Falls through to `_present_recipe` so the result lands in exactly the
    same Save-button flow as a pasted URL.
    """
    if not _is_allowed(update.effective_user.id):
        return

    log.info("Photo received from user %s", update.effective_user.id)
    msg = await update.message.reply_text("📸 Reading recipe…")

    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        image_bytes = await file.download_as_bytearray()
    except Exception:
        log.exception("Photo download failed")
        await msg.edit_text("❌ Couldn't download the photo. Try again.")
        return

    result = await process_recipe_image(bytes(image_bytes))
    if result is None:
        await msg.edit_text(
            "❌ Couldn't read a recipe from that photo.\n"
            "Make sure the photo is in focus, the recipe text is fully "
            "visible, and (if you sent a screenshot) that the OCR model "
            "is configured on the server."
        )
        return

    recipe, url = result
    log.info("Photo OCR ingested: title=%r url=%s", recipe.get("title"), url)
    await _present_recipe(url, recipe, msg)


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


async def _present_recipe(url: str, recipe: dict, msg) -> None:
    """Push a parsed recipe to the display.

    If `url` matches an already-saved row, restore its comments and skip
    the Save prompt. Otherwise stash in the pending map and offer a
    💾 Save button — the DB row is only created when the user taps Save.
    """
    existing = library.find_by_url(url)
    if existing is not None:
        if not push_recipe_to_display(existing):
            await msg.edit_text("❌ Couldn't render that recipe to the display.")
            return
        total = display_state.get()["total_pages"]
        await msg.edit_text(
            _format_push_reply(existing["title"], existing.get("url"), total),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_push_inline_actions(
                recipe_id=existing["id"], pending_token=None
            ),
        )
        return

    try:
        display_state.set_recipe(recipe, comments=[], recipe_id=None, url=url)
    except Exception:
        log.exception("Failed to render recipe %r to display", recipe.get("title"))
        await msg.edit_text("❌ Couldn't render that recipe to the display.")
        return
    total_pages = display_state.get()["total_pages"]

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

    # Refuse uploads with unknown or oversized declared size — download_as_bytearray
    # buffers the whole document in memory, so we need the cap up-front.
    if not doc.file_size or doc.file_size > _JSON_MAX_BYTES:
        await msg.edit_text(
            f"❌ JSON file too large or size unknown (limit "
            f"{_JSON_MAX_BYTES // 1024} KB)."
        )
        return

    try:
        file = await doc.get_file()
        raw = await file.download_as_bytearray()
    except Exception:
        log.exception("Failed to download JSON document")
        await msg.edit_text("❌ Couldn't download the file. Try again.")
        return
    # Double-check actual size in case Telegram's declared size was wrong.
    if len(raw) > _JSON_MAX_BYTES:
        await msg.edit_text(
            f"❌ JSON file too large (limit {_JSON_MAX_BYTES // 1024} KB)."
        )
        return
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
    """User tapped 💾 Save — persist the recipe to the library."""
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
    recipe_id = library.upsert_recipe(url, recipe)
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
        await query.message.reply_text("💾 Saved to library.")


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
    recipe_id = library.upsert_recipe(url, recipe)
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
                text="Your library is empty — save a recipe first.",
            )
            return
        await context.bot.send_message(
            chat_id=chat_id,
            text=_format_surprise_card(row),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_surprise_keyboard(row["id"]),
        )
