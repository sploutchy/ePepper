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
   │                 │ ◄──── low-batt alert  │                          │
   └─────────────────┘                       │  ▸ display state         │
                                             │  ▸ recipe library        │
                                             │    (SQLite + FTS)        │
                                             │  ▸ midnight anniversary  │
                                             │    scheduler             │
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
up periodically if your saved recipes matter.

### Environment Variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `ALLOWED_USERS` | Comma-separated Telegram user IDs. Empty = allow all (only safe for a private bot). |
| `API_KEY` | Shared secret for ESP32 ↔ server auth. Generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `API_PORT` | Server port (default: `8080`). |
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
| `/search <query>` | Full-text search over title + ingredients + notes. Tap a result to push it. |
| `/clear` | Clear the panel (renders a blank white frame). |
| `/status` | Last-known battery / temp / humidity + last-seen age. |
| `/prompt_screenshot` | Copy-paste LLM prompt for converting a photo → JSON-LD. |
| `/prompt_url [URL]` | Copy-paste LLM prompt for fetching a webpage → JSON-LD; the URL is baked in if you provide one. |
| `/start`, `/help` | Show the command list. |

### LLM prompt workflow

When a website isn't covered by recipe-scrapers (or you only have a
photo), the workflow is:

1. Run `/prompt_screenshot` or `/prompt_url https://…` on the bot.
2. Copy the prompt and hand it to an LLM (Claude, ChatGPT, etc.) with
   your screenshot or instructing it to fetch the URL.
3. Save the LLM's JSON output as `recipe.json` and upload that file to
   the bot. `parse_recipe_jsonld` ingests it into the same internal
   shape `process_recipe_url` produces; the library dedupes by URL.

### Battery monitoring

The ESP32 reports its battery, RSSI, and SHT40 readings on every wake
(button or daily timer). When the battery crosses below 3500 mV the
bot pushes a one-shot warning to every `ALLOWED_USERS` recipient.
Hysteresis re-arms only above 3600 mV, so noisy readings don't spam.

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
next tick. No fallback: if no past-year match exists, the display is
left alone.

## API endpoints

All endpoints require `Authorization: Bearer <API_KEY>` or
`?key=<API_KEY>`.

| Method | Path | Description |
|---|---|---|
| GET | `/version` | Current image hash + page info. ESP32 hits this on every wake to decide whether to refetch. |
| GET | `/image` | Current page as a 1-bit BMP. Defaults to the active page. |
| GET | `/image?page=N` | Specific page as BMP. |
| POST | `/page/next` | Advance to next page (wraps around at the end). |
| POST | `/page/prev` | Previous page (wraps around at page 1). |
| POST | `/page/first` | Jump to page 1 (long-press of prev). |
| POST | `/page/last` | Jump to last page (long-press of next). |
| POST | `/device/status?battery_mv=…&rssi=…&temperature_c=…&humidity_pct=…` | ESP32 wake-cycle report. `temperature_c` / `humidity_pct` are optional. May trigger a low-battery Telegram alert. |
| GET | `/device/status` | Last-known wake-cycle report (JSON). |

Every server response carries a standard `Date:` header which the
firmware parses to keep its system clock approximately correct — no
NTP traffic.

## License

MIT
