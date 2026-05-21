# ePepper

A self-hosted e-ink recipe display for the kitchen. A 7.5" panel on the
counter shows the current recipe; you push new recipes to it from a
**web app** or a **Telegram bot**, the panel cycles its pages with
physical buttons, and a midnight scheduler resurfaces year-ago recipes
on their anniversary.

```
   ┌──────────────┐                              ┌──────────────────────────┐
   │  Web app     │  URL / image                 │  Python server           │
   │  (PWA)       │ ───────────────────────────► │  (FastAPI +              │
   │              │  browse, sort, push          │   python-telegram-bot)   │
   ├──────────────┤                              │                          │
   │  Telegram    │  URL / photo                 │  ▸ display state         │
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
  [recipe-scrapers](https://github.com/hhursev/recipe-scrapers), OCRs
  recipe photos via the configured LLM, renders the panel image
  server-side, persists saved recipes + notes to SQLite, runs the anniversary
  scheduler, and exposes the BMP frames + page navigation to the
  firmware.
- **`esp32/`** — PlatformIO firmware for the XIAO ESP32-S3 module on
  the Seeed reTerminal E1001. Wakes only on a button press or a daily
  timer — no schedule-driven polling. The firmware draws nothing on
  its own; every pixel is rendered server-side.

## Features

- **Two control surfaces.** A PWA-installable web app at `/app/` and a
  Telegram bot — pick whichever fits the moment. Both can add recipes
  (URL or image), search the library, add notes, and push to the display.
- **Library.** Saved recipes persist in SQLite with FTS5 full-text
  search over title + ingredients + notes. Sort by "recently cooked"
  (default), "most cooked", or "least recently cooked", filter by
  source (a website, a named cookbook), paginate via infinite scroll.
  A live "on display" badge marks the recipe currently rendered on
  the panel.
- **Source provenance.** Each recipe carries a source — a website
  host or a named cookbook (`cookbook://<name>/<slug>` URLs, produced
  by the screenshot prompt with the LLM inferring `<name>` from
  visible branding in the photo). The source surfaces on the library
  cards, the recipe detail page, the bot's `/status`, and inline on
  the e-ink panel (with the "from" word localised).
- **Anniversary scheduler.** At local midnight, picks a recipe you
  displayed on this calendar day in any past year and pushes it again
  — so a meal you cooked this time last year resurfaces. Falls back to
  Fooby's weekly-inspiration block when no anniversary exists.
- **Device monitoring.** Battery %, Wi-Fi RSSI, ambient temp +
  humidity (SHT40), last-seen freshness — visible on the web status
  page and in `/status` on the bot. One-shot alerts go out when
  battery drops below 3.5 V or the device's daily heartbeat is
  overdue.
- **Backup to Telegram.** The midnight scheduler tick uploads a
  gzipped SQLite snapshot to a configurable Telegram chat — but only
  when the library has changed since the previous upload. Quiet days
  produce no message; busy days produce one. Unlimited versioned
  history at no storage cost.
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

Two input formats, both surfaces accept both:

| Source | What it does |
|---|---|
| **URL** | Fetched + parsed by [recipe-scrapers](https://github.com/hhursev/recipe-scrapers). Tracking params (`utm_*`, `fbclid`, `ref`, `gclid`) are stripped before the URL is canonicalized + deduped. |
| **Image** | OCR'd via the configured LLM, then ingested into the same canonical recipe shape as a URL. Telegram caption / web filename rides along as a context hint so the model can fill `source_name` even when the photo doesn't show the cover. |

**From the web app:** open `/app/add`, paste a URL, or pick a photo.

**From Telegram:** paste a URL into the chat, or send a photo (optional
caption is forwarded to the OCR LLM as a hint).

Adding via the web lands the recipe in the library immediately. Adding
via the Telegram bot pushes to the panel right away; tap 💾 **Save** on
the resulting message to keep it in the library.

### Browsing the library

The web app's home page (`/app/`) is the main browse surface:

- **Search** by title, ingredients, or notes (FTS5, accent-insensitive).
- **Sort** by **Recently cooked** (default), **Most cooked**, or
  **Least recently cooked** — the library tracks every push so what
  surfaces is what you actually cook, not what you once meant to.
- **Filter** to a specific source (Fooby, BBC, a named cookbook, …).
- **Currently-on-display badge.** The recipe live on the panel is
  flagged with a monitor icon next to its row.
- **Source attribution.** Each card carries a `from <Source>` chip
  next to the title — same source name that's shown on the status
  page, the bot's `/status` / `/search` / push confirmations, and the
  e-ink panel itself.

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
for en/de/fr/it), followed by total time and servings on the meta
line. The source is omitted entirely for OCR'd photos that yielded
no `source_name`.

### Editing recipes

From the web app's recipe page: add or remove notes, push to the
display, or delete.

From the bot, after the recipe is on the display:

- `/comment <text>` adds a note. The note doesn't re-push to the panel
  — it'll show up the next time you display the recipe. So adding a
  note doesn't count as cooking it.
- *Push* a saved recipe via `/search` to make it active so `/comment`
  targets it.

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
- The recipe goes into the library automatically (web Add) or as soon
  as you tap 💾 Save on the bot's push message. Every push to the
  panel bumps its cook count.
- A year later, the anniversary scheduler resurfaces it at midnight.
  Or it surfaces sooner via the **Most cooked** sort once you've made
  it a few times.

## Server

### Environment variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather). **Required** — server won't start without it. |
| `API_KEY` | Shared secret for ESP32 ↔ server auth, and the web-UI login. **Required** — server refuses to start if unset or empty. Generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `ALLOWED_USERS` | Comma-separated Telegram user IDs allowed to talk to the bot. Empty = anyone can talk to the bot (only safe for a truly private bot). **Note:** empty also means low-battery and stale-heartbeat alerts have no one to notify and are silently skipped. |
| `API_PORT` | Server port (default: `8080`). |
| `BACKUP_CHAT_ID` | *Optional.* Telegram chat/channel id (e.g. `-1003608522302`) to receive a daily gzipped DB snapshot. The midnight scheduler tick skips the upload when the library hasn't changed since the previous one. Unset = backups disabled. |
| `WEB_URL` | *Optional.* Public URL of the web app (e.g. `https://epepper.example.com`). When set, the bot's `/start` and `/help` include a clickable link to `<WEB_URL>/app/`. |
| `TZ` | Set in `docker-compose.yml`, default `Europe/Zurich`. Drives the midnight anniversary tick and the `saved_at` MM-DD comparison. |
| `DEVICE_WAKE_HOUR_LOCAL` | *Optional, default `6`.* Wall-clock hour (0–23) the e-ink panel aligns its daily timer wake to — so the panel is fresh when you walk into the kitchen at breakfast instead of drifting via a flat 24 h offset from the last button press. The server returns the seconds-until-next-hit as `next_wake_in_s` on every `/version` query. |

### Web app (`/app/`)

The web UI lives at `https://<your-host>/app/`. Server-rendered HTML
+ HTMX partials, no build step, ~50 KB JS bundled locally.

- **Sign in** with the same `API_KEY` the device uses. The login form
  mints a random session token (stored sha256-hashed in the DB, so a
  read of `recipes.db` can't impersonate a session) and sets it in an
  `epepper_auth` cookie (`HttpOnly`, `Secure`, `SameSite=Lax`).
  Sessions slide on every request: active use keeps you signed in, 30
  days idle and you re-authenticate. `Secure` means you must serve
  `/app/` over HTTPS or the login won't stick.
- **Pages:**
  - `/app/` — library list (search, sort, source filter, infinite
    scroll, on-display badge).
  - `/app/add` — URL paste or recipe-photo upload.
  - `/app/recipes/<id>` — recipe detail (notes, push, delete).
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
dependency) **only when you explicitly save them**. The bot's URL/JSON
paste pushes the recipe to the panel and stashes it in memory; tapping
💾 Save commits it to the library. The web Add flow saves the recipe
to the library immediately and leaves the panel alone (click *Display*
to push). An image push is never persisted.

Schema:

- `recipes(id, url, title, parsed_json, lang, saved_at, last_displayed_at, displayed_count, created_at, deleted_at, source)`
- `comments(id, recipe_id, body, created_at)`
- `sessions(token_hash, created_at, expires_at)` — web-app session tokens. Only the sha256 hash is stored.
- `recipes_fts` — FTS5 virtual table over (title, ingredients, notes).

`saved_at` is the canonical "first saved" timestamp — never moves once
set. `last_displayed_at` is bumped every time the row is pushed to the
panel (web *Display* button, bot `/surprise`, `/search` push, anniversary
scheduler, …) and drives the library's "recently cooked" sort + the
anniversary picker. NULL is a first-class state ("never cooked") — rows
in a library upgraded from before this column existed start NULL and only
get populated when something pushes them, so existing recipes show up as
**never cooked** in the library list and on the detail page until you
display one. In the "recently cooked" sort they sink to the bottom; in
the "least recently cooked" sort they float to the top (nothing is more
stale than a recipe you've never cooked).

`displayed_count` is incremented alongside `last_displayed_at`, so the
library knows how many times you've cooked each recipe. The library card
and detail page render `cooked N×, last <when>` (or just `cooked <when>`
after a single cook), where `<when>` is a humanised relative phrase —
`yesterday`, `3 days ago`, `last week`, `last month`, `2 years ago`, etc.
The bot's search results and surprise card share the same wording via
`status_helpers.humanize_date`. There's a **Most cooked** sort option in
the library header. Counts start at 0 for everything on
upgrade; only future pushes accumulate.

Note: pushing a recipe to the panel is the only thing that bumps these
columns. Adding a recipe via the web `/app/add` page just lands it in
the library; the panel doesn't change until you click **Display** on
its detail page. The bot's URL-paste flow is the exception — that's a
"send to display" command by design.

The `url` column carries one of three URL shapes, all of which
participate in the `UNIQUE` index:

- `https://example.com/…` — a normal site URL. Canonicalised
  (lowercase host, dropped tracking params, trimmed trailing slash,
  fragment stripped) before insertion so equivalent links dedupe.
- `cookbook://<name>/<slug>` — a recipe from a paper cookbook. The
  OCR prompt asks the LLM to fill the `name` part from any
  visible branding in the photo and the `slug` from the title;
  e.g. `cookbook://nos-recettes-preferees/crepes-bretonnes`. The
  `<name>` part doubles as the displayed source name (`from
  Nos-recettes-preferees`).
- `jsonld:<12-hex-digits>` — a content-hash fallback for OCR'd
  photos that yielded no usable source name.

`source` mirrors the displayed source name in lowercase
(`fooby`, `nos-recettes-preferees`, …) and is what the library page's
source-filter dropdown matches against. It's derived from the recipe's
URL/scheme on insert and stored as a regular column on `recipes`. URL
canonicalisation and FTS rebuild run idempotently, so a snapshot from
any prior version restores cleanly.

### Anniversary scheduler

An `asyncio` task in `server/scheduler.py` sleeps until the next local
midnight, then picks the most recently-displayed saved recipe whose
`last_displayed_at` (local time) lands on today's MM-DD in any past
year and pushes it to the panel. The anniversary tracks your actual
cooking cadence: a recipe shown on 2025-05-20 resurfaces on 2026-05-20,
regardless of when it was first saved. Re-displaying a recipe later in
the year moves its anniversary to the new date. DST-aware — fall-back
nights run ~25 h, spring-forward nights ~23 h, without drifting away
from local midnight.

Manual pushes during the day win until the next tick.

**Fallback when no anniversary exists:** the scheduler fetches
Fooby's French "Inspirations de la semaine" block from
[fooby.ch/fr.html](https://fooby.ch/fr.html) and picks one recipe URL
rotated by ISO weekday, so each slot reappears on the same weekday
every week. The picked recipe is rendered transiently (not added to
the library); paste its URL into the web app or bot to keep it. If
the Fooby fetch or parse fails, the display is left unchanged.

### Backup to Telegram

If `BACKUP_CHAT_ID` is set, the midnight scheduler tick runs a
"dirty check": if the DB file's mtime is later than the last
successful upload's timestamp (persisted next to the DB), the
server takes an online SQLite snapshot via
`sqlite3.Connection.backup()` (safe under concurrent writes),
gzips it in memory, and posts the bytes as
`recipes_<utc-timestamp>.db.gz` to the configured chat. A day with
no mutations produces no message; a day with a hundred produces
one. The DB-mtime source of truth survives container restarts.

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
- `epepper_auth` session cookie set by the `/app/login` flow (the
  browser path — convenient for poking at `/version` / `/image` after
  signing in).

The previous `?key=<API_KEY>` query-param fallback was removed: uvicorn
records the full path+query in its access log, so any request that used
it leaked the key into container logs.

| Method | Path | Description |
|---|---|---|
| `GET` | `/version` | Current image hash, page info, and `next_wake_in_s` (seconds until the device's next aligned wake at `DEVICE_WAKE_HOUR_LOCAL`). ESP32 hits this on every wake. |
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
| `/comment <text>` | Add a note to the currently-displayed saved recipe. Doesn't re-push to the panel — the note shows on the next display. |
| `/search <query>` | Full-text search over title + ingredients + notes. Tap a number to push. |
| `/surprise` | Push a random saved recipe to the display. |
| `/clear` | Clear the panel (renders a blank white frame). |
| `/status` | Sectioned device + library snapshot — battery %, signal, env sensors, last-seen (with ⚠️ overdue if heartbeat is stale), saved-recipe count, last backup time. |
| `/start` | Brief welcome + how to send recipes. |
| `/help` | Full command reference. |

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
- The `/version` response carries `next_wake_in_s` — the firmware
  uses it to land its next timer wake at `DEVICE_WAKE_HOUR_LOCAL`
  local time (default `06:00`) instead of drifting 24 h from the
  last button press. Server is the source of truth for when, so
  there's no UTC↔local conversion on the device. Falls back to a
  flat 24 h on /version failure or out-of-range values.
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

## Roadmap

Nice-to-have improvements parked for later. Each is worth doing on its
own — none unblock anything else.

- **Ingredient checkboxes.** Tick items off the recipe page while
  shopping or cooking. State lives in `localStorage` (no schema change),
  scoped per recipe id.
- **Serving scaler.** A small `× N` control on the recipe page that
  rewrites quantities in-place ("4 → 6 servings"). Works on numeric
  tokens at the head of each ingredient line; degrades gracefully when
  a quantity is absent.

## License

MIT
