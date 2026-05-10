# ePepper

E-ink recipe display for the kitchen. Send a recipe URL via Telegram, see it rendered on a 7.5" e-ink screen.

## Architecture

```
Telegram Bot ──► Python server ──► renders recipe as B/W image
                                         │
ESP32-S3 (reTerminal E1001) ◄────────────┘
   polls /version, fetches /image
   physical button cycles pages
```

- **`server/`** — Python backend (FastAPI + python-telegram-bot). Parses recipe URLs, renders paginated B/W images, serves them via REST API.
- **`esp32/`** — PlatformIO firmware for XIAO ESP32-S3. Polls the server, displays images on the e-ink panel, supports page cycling via physical button.

## Server Setup

```bash
cd server
cp .env.example .env
# Edit .env with your Telegram bot token and API key
docker compose -f ../docker-compose.yml up -d --build
```

### Environment Variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `ALLOWED_USERS` | Comma-separated Telegram user IDs (empty = allow all) |
| `API_KEY` | Shared secret for ESP32 ↔ server auth |
| `API_PORT` | Server port (default: 8080) |

## Firmware Setup

### Prerequisites

- [PlatformIO CLI](https://docs.platformio.org/en/latest/core/installation/index.html) or the VS Code extension
- USB-C cable
- XIAO ESP32-S3 with reTerminal E1001

### Configure

Edit `esp32/include/config.h` before flashing:

```c
#define WIFI_SSID     "your-wifi"
#define WIFI_PASSWORD "your-password"
#define SERVER_URL    "https://your-server.example.com"
#define API_KEY       "your-api-key"  // must match server .env
```

Generate an API key:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Flash

```bash
cd esp32
pio run -t upload
```

Monitor serial output:

```bash
pio device monitor -b 115200
```

### Pin Mapping (reTerminal E1001)

From the [official schematic](https://files.seeedstudio.com/wiki/reterminal_e10xx/res/202004307_reTerminal_E1001_V1_2_SCH_251120.pdf) (CC BY-SA 4.0):

| Function | GPIO | Notes |
|---|---|---|
| **Buttons** | | Active-low, 10K pull-up, 100nF debounce |
| KEY0 — Refresh | 3 | Right (green) button |
| KEY1 — Next page | 4 | Middle button |
| KEY2 — Prev page | 5 | Left button |
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
| **I2C (bus 0)** | | SHT40 (0x44), PCF8563 RTC (0x51) |
| SDA | 19 | |
| SCL | 20 | |
| **SD Card** | | |
| SD CS | 14 | |
| SD enable | 16 | |
| SD detect | 15 | |

## Usage

1. Start a chat with your Telegram bot
2. Send `/start` for help
3. Paste a recipe URL — the bot parses it and renders it to the display
4. Use the inline buttons in Telegram or the physical button on the device to cycle pages

## API Endpoints

All endpoints require `Authorization: Bearer <API_KEY>` or `?key=<API_KEY>`.

| Method | Path | Description |
|---|---|---|
| GET | `/version` | Current image hash + page info (ESP32 polls this) |
| GET | `/image` | Current page as BMP (defaults to active page) |
| GET | `/image?page=N` | Specific page as BMP |
| POST | `/page/next` | Advance to next page (wraps around) |
| POST | `/page/prev` | Go to previous page (wraps around) |
| POST | `/device/status` | ESP32 reports battery/RSSI/uptime |
| GET | `/device/status` | Last known device status |

## License

MIT
