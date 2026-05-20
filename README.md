# ePepper

A self-hosted e-ink recipe display for the kitchen. A 7.5" panel on the
counter shows the current recipe; you push new recipes to it from a
**web app** or a **Telegram bot**, the panel cycles its pages with
physical buttons, and a midnight scheduler resurfaces year-ago recipes
on their anniversary.

```
   ┌──────────────┐                              ┌──────────────────────────┐
   │  Web app     │  URL / image / JSON-LD       │  Python server           │
   │  (PWA)       │ ───────────────────────────► │  (FastAPI +              │
   │              │  browse, sort, rate, push    │   python-telegram-bot)   │
   ├──────────────┤                              │                          │
   │  Telegram    │  URL / photo / .json         │  ▸ display state         │
   │  bot         │ ───────────────────────────► │  ▸ recipe library        │
   │              │  /search /status /comment    │    (SQLite + FTS)        │
   └──────────────┘ ◄── alerts + backup ──────── │  ▸ anniversary +         │
                                                 │    Fooby fallback        │
                                                 │  ▸ heartbeat scheduler   │
                                                 └─────────────┬────────────┘
                                                               │ GET  /version
                                                               │ GET  /image (BMP)
                                                               │ POST /device/status
                                                               ▼
                                                 ┌──────────────────────────┐
                                                 │  ESP32-S3 +              │
                                                 │  reTerminal E1001        │
                                                 │  (7.5" UC8179 e-paper)   │
                                                 │                          │
                                                 │  wakes on button or      │
                                                 │  daily timer; posts      │
                                                 │  battery + SHT40 + RSSI, │
                                                 │  fetches the BMP         │
                                                 └──────────────────────────┘
```

- **`server/`** — Python backend. Parses recipe URLs via
  [recipe-scrapers](https://github.com/hhursev/recipe-scrapers), accepts
  schema.org Recipe JSON-LD uploads, renders the panel image
  server-side, persists ratings + notes to SQLite, runs the anniversary
  scheduler, and exposes the BMP frames + page navigation to the
  firmware.
- **`esp32/`** — PlatformIO firmware for the XIAO ESP32-S3 module on
  the Seeed reTerminal E1001. Wakes only on a button press or a daily
  timer — no schedule-driven polling. The firmware draws nothing on
  its own; every pixel is rendered server-side.

## Features

- **Two control surfaces.** A PWA-installable web app at `/app/` and a
  Telegram bot — pick whichever fits the moment. Both can add recipes
  (URL / image / JSON-LD), search the library, change ratings, add
  notes, and push to the display.
- **Library.** Saved recipes persist in SQLite with FTS5 full-text
  search over title + ingredients + notes. Sort by recency or rating,
  filter by minimum rating or source (a website, a named cookbook),
  paginate via infinite scroll. A live "on display" badge marks the
  recipe currently rendered on the panel.
- **Source provenance.** Each recipe carries a source — a website
  host or a named cookbook (`cookbook://<name>/<slug>` URLs, produced
  by the screenshot prompt with the LLM inferring `<name>` from
  visible branding in the photo). The source surfaces on the library
  cards, the recipe detail page, the bot's `/status`, and inline on
  the e-ink panel (with the "from" word localised).
- **Anniversary scheduler.** At local midnight, picks a recipe whose
  saved-at calendar day matches today (any past year) and pushes it.
  Falls back to Fooby's weekly-inspiration block when no anniversary
  exists.
- **Device monitoring.** Battery %, Wi-Fi RSSI, ambient temp +
  humidity (SHT40), last-seen freshness — visible on the web status
  page and in `/status` on the bot. One-shot alerts go out when
  battery drops below 3.5 V or the device's daily heartbeat is
  overdue.
- **Backup to Telegram.** Every library mutation triggers a debounced
  gzipped SQLite snapshot to a configurable Telegram chat — unlimited
  versioned history at no storage cost.
- **Dark mode.** Follows the OS preference automatically (with a
  manual toggle in the header), including matching mobile browser
  chrome.
- **Live display preview.** The web status page renders the panel's
  current 800 × 480 frame so you can see what's showing without
  walking to the kitchen.

## Installation

### Prerequisites

You'll need:

1. A Linux/Mac host that can run Docker (the server lives in a
   container).
2. A reverse proxy in front of it terminating TLS — the web app sets
   a `Secure` cookie, so HTTPS is required for login. Caddy, nginx,
   Traefik, anything that does ACME.
3. A Telegram bot token (free, from [@BotFather](https://t.me/BotFather)).
   *Note:* the bot is currently a hard dependency of the server; even
   if you only want to use the web app, the server won't start
   without a token.
4. *Optional, for the e-ink display itself:* a Seeed reTerminal
   E1001 (XIAO ESP32-S3 + 7.5" UC8179 e-paper). The server is useful
   without it — the web app fully replaces the panel for browsing —
   but the device is the project's headline use case.

### Server

```bash
cd server
cp .env.example .env
# Edit .env: paste your Telegram bot token, an API_KEY you generate, and
# your Telegram user id in ALLOWED_USERS.
docker compose -f ../docker-compose.yml up -d --build
```

Generate a strong `API_KEY` once:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

The container mounts `./data:/app/data` for the SQLite library. Set
up `BACKUP_CHAT_ID` (see below) for off-host versioned backups.

Runtime: Python 3.12 in the bundled Dockerfile.

### Reverse proxy

Point your reverse proxy at the container's `API_PORT` (default
`8080`) and terminate TLS in front. The web app at
`https://<your-host>/app/` will sign in fine over HTTPS; the
device-facing endpoints (`/version`, `/image`, `/device/status`) use
Bearer auth and don't depend on TLS, but you should still proxy them
over HTTPS.

### Firmware

See [Firmware](#firmware) below if you have the reTerminal E1001.
The web app and bot work fine without the device — they just don't
have anywhere to push the rendered image.

## Usage

### Adding a recipe

Three input formats, both surfaces accept all three:

| Source | What it does |
|---|---|
| **URL** | Fetched + parsed by [recipe-scrapers](https://github.com/hhursev/recipe-scrapers). Tracking params (`utm_*`, `fbclid`, `ref`, `gclid`) are stripped before the URL is canonicalized + deduped. |
| **Image** | Resized and Floyd–Steinberg-dithered to the panel's 800 × 480 1-bit canvas. **Not saved to the library** — image pushes are ephemeral display content. |
| **JSON-LD** | A `.json` file containing a `schema.org/Recipe` object. Use this when recipe-scrapers can't handle a site, or to OCR a photo via an LLM (see [LLM prompt workflow](#llm-prompt-workflow)). |

**From the web app:** open `/app/add`, paste a URL, or pick a file.

**From Telegram:** paste a URL into the chat, send a photo, or upload
a `.json` document.

A URL-or-JSON push lands the recipe in the library *unrated*; it
appears in `/app/recipes/<id>` (and in `/status`) but stays out of the
library list until you give it a 1–5 star rating.

### Browsing the library

The web app's home page (`/app/`) is the main browse surface:

- **Search** by title, ingredients, or notes (FTS5, accent-insensitive).
- **Sort** by recency (default), highest rated, lowest rated, or oldest.
- **Filter** to a minimum rating (2★+, 3★+, …) or to a specific source
  (Fooby, BBC, a named cookbook, …).
- **Currently-on-display badge.** The recipe live on the panel is
  flagged with a monitor icon next to its row.
- **Source attribution.** Each card carries a `from <Source>` chip
  next to the title (desktop ≥ 640 px) — the same source name that's
  shown on the status page, the bot's `/status`, and the e-ink panel
  itself.

In the bot, `/search <query>` returns the top 5 results as a tappable
keyboard.

### Pushing to the display

From the web app, open a recipe and click **Display**. From the bot,
tap the result number in a `/search` reply, or paste a URL the
library already knows.

The panel renders title + ingredients + numbered steps across as
many pages as fit (a tall recipe might be 2–3 pages). Notes get
their own trailing page. The header carries `Title from Source —
page X/Y` (with the `from` word localised — `from`/`aus`/`de`/`da`
for en/de/fr/it), followed by total time, servings, and the rating
stars on the meta line. The source is omitted entirely for image
uploads and JSON-LD recipes without a URL.

### Editing recipes

From the web app's recipe page: change the rating with the star
buttons, add or remove notes, push to the display, or delete.

From the bot, after the recipe is on the display:

- `/rate <1–5>` updates the rating.
- `/comment <text>` adds a note.
- *Push* a saved recipe via `/search` to make it active so the above
  commands target it.

Deletes are soft (the row is hidden via `deleted_at` with no UI
restore). If you genuinely need a deleted recipe back, pull it from
the most recent Telegram backup snapshot or clear `deleted_at` in
SQL.

### Cycling pages on the device

Three physical buttons:

- **Refresh** (right / green): short — re-fetch; long press — force a
  full panel redraw.
- **Next** (middle): short — next page; long press — jump to last.
- **Prev** (left): short — previous page; long press — jump to first.
- **Prev + Refresh chord:** clear the display (renders an idle hint).

### Day-to-day rhythm

- Tap a recipe URL into the web app or the bot; it lands on the panel.
- Cook from the panel — buttons cycle pages.
- After cooking, give it a rating (web stars or `/rate` in the bot).
  That moves it into the library proper.
- A year later, the anniversary scheduler resurfaces it at midnight.
  Or it shows up again because you ranked it 5★ and now sort by
  rating.

## Server

### Environment variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather). **Required** — server won't start without it. |
| `API_KEY` | Shared secret for ESP32 ↔ server auth, and the web-UI login. **Required** — server refuses to start if unset or empty. Generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `ALLOWED_USERS` | Comma-separated Telegram user IDs allowed to talk to the bot. Empty = anyone can talk to the bot (only safe for a truly private bot). **Note:** empty also means low-battery and stale-heartbeat alerts have no one to notify and are silently skipped. |
| `API_PORT` | Server port (default: `8080`). |
| `BACKUP_CHAT_ID` | *Optional.* Telegram chat/channel id (e.g. `-1003608522302`) to receive a gzipped DB snapshot after every library mutation. Unset = backups disabled. |
| `BACKUP_DEBOUNCE_S` | Coalesce mutation bursts into one snapshot upload (default: `60`). |
| `TZ` | Set in `docker-compose.yml`, default `Europe/Zurich`. Drives the midnight anniversary tick and the `saved_at` MM-DD comparison. |

### Web app (`/app/`)

The web UI lives at `https://<your-host>/app/`. Server-rendered HTML
+ HTMX partials, no build step, ~50 KB JS bundled locally.

- **Sign in** with the same `API_KEY` the device uses. The login form
  sets an `epepper_auth` cookie (`HttpOnly`, `Secure`,
  `SameSite=Lax`, 365-day max-age) — `Secure` means you must serve
  `/app/` over HTTPS or the login won't stick.
- **Pages:**
  - `/app/` — library list (search, sort, min-rating filter,
    infinite scroll, on-display badge).
  - `/app/add` — URL paste, image upload, or JSON-LD upload.
  - `/app/recipes/<id>` — recipe detail (rating, notes, push,
    delete).
  - `/app/status` — live panel preview, panel state, library
    stats + last backup, device readings.
- **Dark mode.** Follows OS preference automatically; a `☀/☾`
  toggle in the header overrides and persists in `localStorage`.
  Mobile browser chrome (URL bar, status bar) flips with the page
  via `theme-color` metas.
- **PWA.** A `manifest.webmanifest` + service worker at `/app/sw.js`
  let you install to the home screen as a standalone app. The SW
  pre-caches the static shell (`app.css`, `htmx.min.js`, the pepper
  icon, the manifest); HTML and API responses are always
  network-first so the library never goes stale.

The login cookie also unlocks `/version`, `/image`, etc. for the
browser, so you can debug the device by opening those URLs after
signing in.

### Recipe library (SQLite)

Recipes persist to `data/recipes.db` (stdlib `sqlite3`, no extra
dependency) **only when you explicitly save them**. A pushed but
unrated URL/JSON-LD recipe sits in the DB invisibly until you pick a
rating; an image push is never persisted.

Schema:

- `recipes(id, url, title, parsed_json, lang, rating, saved_at, created_at, deleted_at, source)`
- `comments(id, recipe_id, body, created_at)`
- `recipes_fts` — FTS5 virtual table over (title, ingredients, notes).

The `url` column carries one of three URL shapes, all of which
participate in the `UNIQUE` index:

- `https://example.com/…` — a normal site URL. Canonicalised
  (lowercase host, dropped tracking params, trimmed trailing slash,
  fragment stripped) before insertion so equivalent links dedupe.
- `cookbook://<name>/<slug>` — a recipe from a paper cookbook. The
  screenshot prompt asks the LLM to fill the `name` part from any
  visible branding in the photo and the `slug` from the title;
  e.g. `cookbook://nos-recettes-preferees/crepes-bretonnes`. The
  `<name>` part doubles as the displayed source name (`from
  Nos-recettes-preferees`).
- `jsonld:<12-hex-digits>` — a content-hash fallback for JSON-LD
  uploads that arrived without a `url` field.

`source` mirrors the displayed source name in lowercase
(`fooby`, `nos-recettes-preferees`, …) and is what the library page's
source-filter dropdown matches against. It's backfilled from existing
URLs on startup by `_migrate_add_source`. URL canonicalisation,
source backfill, and FTS rebuild all run idempotently, so a snapshot
from any prior version restores cleanly.

### Anniversary scheduler

An `asyncio` task in `server/scheduler.py` sleeps until the next
local midnight, then picks the most recently-saved recipe whose
`saved_at` (local time) lands on today's MM-DD in any past year and
pushes it to the panel. DST-aware — fall-back nights run ~25 h,
spring-forward nights ~23 h, without drifting away from local
midnight.

Manual pushes during the day win until the next tick.

**Fallback when no anniversary exists:** the scheduler fetches
Fooby's French "Inspirations de la semaine" block from
[fooby.ch/fr.html](https://fooby.ch/fr.html) and picks one recipe URL
rotated by ISO weekday, so each slot reappears on the same weekday
every week. The picked recipe is rendered transiently (not added to
the library); paste its URL into the web app or bot to keep it. If
the Fooby fetch or parse fails, the display is left unchanged.

### Backup to Telegram

If `BACKUP_CHAT_ID` is set, every library mutation (save / rate /
comment / delete) triggers an online SQLite snapshot (via
`sqlite3.Connection.backup()`, safe under concurrent writes), gzips
it in memory, and posts the bytes as a `recipes_<utc-timestamp>.db.gz`
document to that chat. Bursts within `BACKUP_DEBOUNCE_S` (default
60 s) coalesce into a single upload.

**Recommended setup:** a private Telegram channel "ePepper backups"
with the bot added as admin (`Post Messages` permission). Files sent
to normal Telegram chats persist indefinitely, so the channel doubles
as unlimited versioned history at no storage cost.

The web status page and the bot's `/status` both surface the
timestamp of the most recent successful upload.

**Restore from a snapshot:** stop the container, drop the snapshot in
place, and start again:

```bash
docker compose stop epepper
gunzip -c recipes_<timestamp>.db.gz > ./data/recipes.db
docker compose start epepper
```

### API endpoints

All endpoints require one of:

- `Authorization: Bearer <API_KEY>` header (the firmware uses this).
- `?key=<API_KEY>` query param (handy in the browser, but the key
  ends up in proxy logs / browser history / Referer — use sparingly).
- `epepper_auth` cookie set by the `/app/login` flow.

| Method | Path | Description |
|---|---|---|
| `GET` | `/version` | Current image hash + page info. ESP32 hits this on every wake to decide whether to refetch. |
| `GET` | `/image` | Current page as a 1-bit BMP. Defaults to the active page. |
| `GET` | `/image?page=N` | Specific page as BMP. |
| `POST` | `/page/next` | Advance to next page (wraps at the end). |
| `POST` | `/page/prev` | Previous page (wraps at page 1). |
| `POST` | `/page/first` | Jump to page 1 (long-press of prev). |
| `POST` | `/page/last` | Jump to last page (long-press of next). |
| `POST` | `/display/clear` | Clear the panel to the idle frame. Fired by the device's PREV + REFRESH chord and by the bot's `/clear`. |
| `POST` | `/device/status?battery_mv=…&rssi=…&temperature_c=…&humidity_pct=…` | ESP32 wake-cycle report. `temperature_c` / `humidity_pct` are optional. May trigger a low-battery alert. |
| `GET` | `/device/status` | Last-known wake-cycle report (JSON). |

Every server response carries a standard `Date:` header which the
firmware parses to keep its system clock approximately correct — no
NTP traffic.

## Telegram bot

The bot is the secondary control surface, optimised for "send a URL
from your phone while you're standing in a bookstore" style flows.
Everything it can do the web app can also do — the bot's only unique
strengths are *(a)* speaking from anywhere without a browser session
and *(b)* receiving the device-health alerts.

### Commands

| Command | What it does |
|---|---|
| `/recipe <url>` | Force-parse a URL even if the on_text fallback misreads it. |
| `/comment <text>` | Add a note to the currently-displayed *saved* recipe. |
| `/rate <1-5>` | Change the rating of the currently-displayed saved recipe. |
| `/search <query>` | Full-text search over title + ingredients + notes. Tap a number to push. |
| `/clear` | Clear the panel (renders a blank white frame). |
| `/status` | Sectioned device + library snapshot — battery %, signal, env sensors, last-seen (with ⚠️ overdue if heartbeat is stale), saved-recipe count, last backup time. |
| `/prompt_screenshot` | Copy-paste LLM prompt for converting a photo → JSON-LD. |
| `/prompt_url [URL]` | Copy-paste LLM prompt for fetching a webpage → JSON-LD; the URL is baked in if you provide one. |
| `/start` | Brief welcome + how to send recipes. |
| `/help` | Full command reference. |

### LLM prompt workflow

When recipe-scrapers can't handle a site (or you only have a photo):

1. Run `/prompt_screenshot` or `/prompt_url https://…` in the bot.
2. Copy the prompt and hand it to an LLM (Claude, ChatGPT, Perplexity,
   …) with your screenshot or instructing it to fetch the URL.
3. Upload the resulting `recipe.json` to the bot or the web app's
   *Add* page. `parse_recipe_jsonld` ingests it into the same shape
   `process_recipe_url` produces; the library dedupes by source URL
   (or by a synthetic hash if the JSON-LD has no `url` field).

### Device health alerts

The ESP32 reports battery, RSSI, and SHT40 readings on every wake.
Two one-shot alerts go to every `ALLOWED_USERS` recipient:

- **Low battery** — fires the first POST below 3 500 mV; hysteresis
  re-arms only above 3 600 mV so noisy readings don't spam. Driven
  reactively by the `/device/status` POST.
- **Stale heartbeat** — fires when the device hasn't checked in for
  ≥ 25 h. Driven proactively by an hourly scheduler, because the
  absence of POSTs is exactly what we're detecting. Re-armed by the
  next successful POST.

Both conditions also appear inline on the web status page and in the
bot's `/status` — battery icon flips from 🔋 to 🪫, last-seen line
appends ⚠️ overdue.

## Firmware

### Hardware

XIAO ESP32-S3 mounted on a Seeed reTerminal E1001 carrier with a 7.5"
UC8179 e-paper panel. The firmware is intentionally minimal: wake,
talk to the server, sleep. Every pixel is rendered server-side, so
firmware updates are rarely needed — most changes happen in the
Python rendering pipeline.

### Prerequisites

- [PlatformIO CLI](https://docs.platformio.org/en/latest/core/installation/index.html) or the VS Code extension
- USB-C cable
- XIAO ESP32-S3 in a Seeed reTerminal E1001 (UC8179 7.5" e-ink)

### Configure

`esp32/include/config.h` is gitignored. Copy the template, then edit
WiFi credentials, the server URL, and the API key:

```bash
cp esp32/include/config.h.example esp32/include/config.h
# then edit:
#   WIFI_SSID / WIFI_PASSWORD
#   SERVER_URL (use https:// — the firmware doesn't enforce it but you should)
#   API_KEY     (must match server .env)
```

### Flash + monitor

```bash
cd esp32
pio run -t upload
pio device monitor -b 115200
```

### Behavior

- Wakes on a button press or the daily timer (no schedule-driven
  polling).
- On wake: pings `/version`, compares the hash; if changed, refetches
  the current page's BMP and redraws.
- Posts a `/device/status` report on every wake — battery mV, RSSI,
  SHT40 temp + humidity (if present).
- Keeps approximate wall-clock time from the HTTP `Date:` header on
  every response, no NTP traffic.

### Pin mapping (reTerminal E1001)

From the [official schematic](https://files.seeedstudio.com/wiki/reterminal_e10xx/res/202004307_reTerminal_E1001_V1_2_SCH_251120.pdf) (CC BY-SA 4.0):

| Function | GPIO | Notes |
|---|---|---|
| **Buttons** | | Active-low, 10K pull-up, 100nF debounce |
| KEY0 — Refresh | 3 | Right (green) — short: refresh, long: force full redraw |
| KEY1 — Next page | 4 | Middle — short: next, long: jump to last page |
| KEY2 — Prev page | 5 | Left — short: prev, long: jump to first page |
| **Display (SPI)** | | UC8179 via 50P FPC |
| EPD CLK | 7 | |
| EPD MOSI | 9 | |
| EPD CS | 10 | |
| EPD DC | 11 | |
| EPD RST | 12 | |
| EPD BUSY | 13 | |
| **Peripherals** | | |
| Status LED | 6 | Active-low (green) |
| Buzzer | 45 | Active-high, MLT-8530 piezo |
| Battery ADC | 1 | Enable via GPIO21 |
| Battery enable | 21 | High to read ADC |
| **I²C (bus 0)** | | SHT40 used; PCF8563 RTC present but unused |
| SDA | 19 | |
| SCL | 20 | |
| SHT40 ambient | 0x44 | Temp + humidity, read on every wake |
| PCF8563 RTC | 0x51 | *Present on board, not used* — time is synced from the HTTP `Date:` header instead. |
| **SD card** | | *Present on board, not used by firmware.* |
| SD CS | 14 | |
| SD enable | 16 | |
| SD detect | 15 | |

## License

MIT
