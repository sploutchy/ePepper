# ePepper

E-ink recipe display for the kitchen. Push a recipe to a Telegram bot,
see it rendered on a 7.5" e-ink panel; pull it back later from a small
saved library or let yesterday's anniversary recipe surface itself.

## Architecture

```
   ┌─────────────────┐                       ┌──────────────────────────┐
   │  Telegram bot   │  URL / photo / .json  │  Python server           │
   │                 │ ────────────────────► │  (FastAPI +              │
   │                 │  /search  /status     │   python-telegram-bot)   │
   │                 │ ◄── alerts + backup   │                          │
   └─────────────────┘                       │  ▸ display state         │
                                             │  ▸ recipe library        │
                                             │    (SQLite + FTS)        │
                                             │  ▸ anniversary scheduler │
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
                                             │  wakes on a button press │
                                             │  or the daily timer;     │
                                             │  posts battery + SHT40   │
                                             │  + RSSI, fetches the BMP │
                                             └──────────────────────────┘
```

- **`server/`** — Python backend. Parses recipe URLs via
  [recipe-scrapers](https://github.com/hhursev/recipe-scrapers), accepts
  schema.org Recipe JSON-LD uploads, renders the panel image (incl. the
  top button glyphs and the page indicator), persists ratings + notes
  to SQLite, and pushes a different recipe at local midnight if today's
  date matches a previous-year save.
- **`esp32/`** — PlatformIO firmware for the XIAO ESP32-S3 module on
  the Seeed reTerminal E1001. Wakes only on a button press or a daily
  timer — no schedule-driven polling. The firmware draws nothing on
  its own; every pixel is rendered server-side.

## Server Setup

```bash
cd server
cp .env.example .env
# Edit .env with your Telegram bot token, allowed user IDs, and API key.
docker compose -f ../docker-compose.yml up -d --build
```

The container mounts `./data:/app/data` for the SQLite library; back it
up periodically if your saved recipes matter (or use the built-in
Telegram-chat backup — see [Backup to Telegram](#backup-to-telegram)
below).

Runtime: Python 3.12 in the bundled Dockerfile. The web UI sets a
`Secure` session cookie, so the `/app/` routes effectively require
HTTPS — terminate TLS at a reverse proxy (Caddy, nginx, Traefik, …)
in front of the container.

### Environment Variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather). **Required** — server won't start without it. |
| `API_KEY` | Shared secret for ESP32 ↔ server auth, and the web-UI login. **Required** — server refuses to start if unset or empty. Generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `ALLOWED_USERS` | Comma-separated Telegram user IDs. Empty = anyone can talk to the bot (only safe for a truly private bot). **Note:** empty also means low-battery and stale-heartbeat alerts have no one to notify and are silently skipped. |
| `API_PORT` | Server port (default: `8080`). |
| `BACKUP_CHAT_ID` | *Optional.* Telegram chat/channel id (e.g. `-1003608522302`) to receive a gzipped DB snapshot after every library mutation. Unset = backups disabled. |
| `BACKUP_DEBOUNCE_S` | Coalesce mutation bursts into one snapshot upload (default: `60`). |
| `TZ` | Set in `docker-compose.yml`, default `Europe/Zurich`. Drives the midnight anniversary tick and the `saved_at` MM-DD comparison. |

## Firmware Setup

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

### Pin Mapping (reTerminal E1001)

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

## Usage

1. Start a chat with your bot. `/start` lists the available commands.
2. Push a recipe by one of:
   - Pasting a recipe URL (handled by recipe-scrapers).
   - Sending a photo of a printed recipe (scaled + dithered, no OCR).
   - Uploading a `.json` file with a schema.org Recipe (for sites
     recipe-scrapers doesn't cover, and for OCR via an LLM — see
     `/prompt_screenshot` and `/prompt_url`).
3. Cycle pages on the device with the **physical buttons**:
   - Short press: refresh / next / prev.
   - Long press (≥ 500 ms): force a full redraw (refresh button), or
     jump to the last/first page (next/prev).
4. Tap **💾 Save** under a pushed recipe, then a 1–5 star rating, to
   keep it in the library.
5. `/comment <text>` adds a free-text note to a saved recipe — notes
   appear as an extra "Notes" page after the recipe pages.

### Telegram commands

| Command | What it does |
|---|---|
| `/recipe <url>` | Force-parse a URL even if the on_text fallback misreads it. |
| `/comment <text>` | Add a note to the currently-displayed *saved* recipe. |
| `/rate <1-5>` | Change the rating of the currently-displayed saved recipe. |
| `/search <query>` | Full-text search over title + ingredients + notes. Tap a number to push it. |
| `/clear` | Clear the panel (renders a blank white frame). |
| `/status` | Sectioned device + library snapshot — battery %, signal, env sensors, last-seen (with ⚠️ overdue if heartbeat is stale), saved-recipe count. |
| `/prompt_screenshot` | Copy-paste LLM prompt for converting a photo → JSON-LD. |
| `/prompt_url [URL]` | Copy-paste LLM prompt for fetching a webpage → JSON-LD; the URL is baked in if you provide one. |
| `/start` | Brief welcome + how to send recipes. |
| `/help` | Full command reference (this list, in-bot). |

### LLM prompt workflow

When a website isn't covered by recipe-scrapers (or you only have a
photo), the workflow is:

1. Run `/prompt_screenshot` or `/prompt_url https://…` on the bot.
2. Copy the prompt and hand it to an LLM (Claude, ChatGPT, etc.) with
   your screenshot or instructing it to fetch the URL.
3. Upload the `recipe.json` to the bot — either the file the assistant
   offers as a download (the prompt asks for it explicitly), or save
   its code block to a file yourself. `parse_recipe_jsonld` ingests it
   into the same internal shape `process_recipe_url` produces; the
   library dedupes by URL.

### Device health alerts

The ESP32 reports its battery, RSSI, and SHT40 readings on every wake
(button or daily timer). Two one-shot alerts go out to every
`ALLOWED_USERS` recipient:

- **Low battery** — fires the first POST that comes in below 3500 mV;
  hysteresis re-arms only above 3600 mV so noisy readings don't spam.
  Driven reactively by the `/device/status` POST.
- **Stale heartbeat** — fires when the device hasn't checked in for
  ≥25 h (24 h daily-timer cadence + an hour of buffer for clock drift /
  Wi-Fi reconnect). Driven proactively by an hourly scheduler in
  `server/scheduler.py`, because the absence of POSTs is the signal.
  Re-armed by the next successful POST.

Both conditions are also surfaced inline in `/status` — the device
section shows `🪫` instead of `🔋` for low battery, and appends
`⚠️ overdue` to the "Last seen" line when the heartbeat is stale.

## Recipe library

Recipes persist to a small SQLite DB at `data/recipes.db` (stdlib
`sqlite3`, no extra dependency) **only when you explicitly save them**
— tap **💾 Save** under the push message, then pick a 1–5 star rating.
A pushed-but-unsaved recipe is held in memory only and is lost on
container restart.

Schema:

- `recipes(id, url, title, parsed_json, lang, rating, saved_at, created_at)`
- `comments(id, recipe_id, body, created_at)`
- `recipes_fts` — FTS5 virtual table over (title, ingredients, notes).

URLs are canonicalized (lowercase host, dropped tracking params, etc.)
before they hit the unique index, so equivalent links dedupe cleanly.

### Anniversary scheduler

`server/scheduler.py` runs an `asyncio` task that sleeps until the next
local midnight, then picks the most recently-saved recipe whose
`saved_at` (local time) lands on today's MM-DD in any past year and
pushes it to the panel. Manual pushes during the day win until the
next tick. DST-aware: the sleep uses timezone-aware arithmetic, so
fall-back nights run ~25 h and spring-forward nights ~23 h without
drifting away from local midnight.

**Fallback when no anniversary exists:** the scheduler fetches Fooby's
French "Inspirations de la semaine" block from
[fooby.ch/fr.html](https://fooby.ch/fr.html) and picks one recipe URL
rotated by ISO weekday, so each slot reappears on the same weekday
every week. The picked recipe is rendered transiently (not added to
the library); to keep it, paste its URL into the bot or the web UI's
*Add* page. If the Fooby fetch or parse fails, the display is left
unchanged.

### Web UI

A small server-rendered web UI lives at `https://<your-host>/app/` for
browsing, searching, deleting, re-rating, commenting, and pushing
recipes to the panel from a browser. Built with Jinja2 + HTMX (no
build step, ~50 KB JS dependency bundled locally).

- **Sign in** with the same `API_KEY` the device uses. The login form
  sets an `epepper_auth` cookie (`HttpOnly`, `Secure`, `SameSite=Lax`,
  365-day max-age) — `Secure` means you must serve `/app/` over
  HTTPS or the login won't stick.
- **Browse / search** the saved library, with infinite scroll.
- **Add a recipe** at `/app/add` — paste a URL, drop a photo (≤ 8 MB,
  panel-only), or upload a schema.org Recipe JSON-LD file (≤ 256 KB,
  parsed into the library).
- **Recipe page** lets you change the rating (star buttons),
  add/remove notes, push to the panel, or delete.
- **Delete is soft** — the row is hidden via a `deleted_at` timestamp
  and an undo toast appears on the index for ~8 seconds. Hard-deleting
  is a manual SQL job (intentional; the recipe library is precious).

The same cookie auth also unlocks `/version`, `/image`, etc. for the
browser, so you can debug the device by opening those URLs after
signing in to `/app/`.

### Backup to Telegram

If `BACKUP_CHAT_ID` is set, every library mutation (`/save`, `/rate`,
`/comment`) triggers an online SQLite snapshot (via
`sqlite3.Connection.backup()`, safe under concurrent writes), gzips it
in memory, and posts the bytes as a `recipes_<utc-timestamp>.db.gz`
document to that chat. Bursts within `BACKUP_DEBOUNCE_S` (default 60 s)
coalesce into a single upload so a `/save` + `/rate` + `/comment` flow
produces one snapshot rather than three.

Recommended setup: a private Telegram channel "ePepper backups" with
the bot added as admin (`Post Messages` permission). Files sent to
normal Telegram chats persist indefinitely, so the channel doubles as
unlimited versioned history at no storage cost.

**Restore from a snapshot:** stop the container, drop the snapshot in
place, and start again:

```bash
docker compose stop epepper
gunzip -c recipes_<timestamp>.db.gz > ./data/recipes.db
docker compose start epepper
```

URL canonicalization and the FTS index both run idempotent migrations
on startup, so a snapshot from any prior version restores cleanly.

## API endpoints

All endpoints require one of:
- `Authorization: Bearer <API_KEY>` header (the firmware uses this)
- `?key=<API_KEY>` query param (handy in the browser, but the key
  ends up in proxy logs / browser history / Referer — use sparingly)
- `epepper_auth` cookie set by the `/app/login` flow

| Method | Path | Description |
|---|---|---|
| GET | `/version` | Current image hash + page info. ESP32 hits this on every wake to decide whether to refetch. |
| GET | `/image` | Current page as a 1-bit BMP. Defaults to the active page. |
| GET | `/image?page=N` | Specific page as BMP. |
| POST | `/page/next` | Advance to next page (wraps around at the end). |
| POST | `/page/prev` | Previous page (wraps around at page 1). |
| POST | `/page/first` | Jump to page 1 (long-press of prev). |
| POST | `/page/last` | Jump to last page (long-press of next). |
| POST | `/display/clear` | Clear the panel to the idle frame. Fired by the device's PREV + REFRESH chord, and by the bot's `/clear` command. |
| POST | `/device/status?battery_mv=…&rssi=…&temperature_c=…&humidity_pct=…` | ESP32 wake-cycle report. `temperature_c` / `humidity_pct` are optional. May trigger a low-battery Telegram alert. |
| GET | `/device/status` | Last-known wake-cycle report (JSON). |

Every server response carries a standard `Date:` header which the
firmware parses to keep its system clock approximately correct — no
NTP traffic.

## License

MIT
