"""ePepper configuration — all from env vars."""

import os
from zoneinfo import ZoneInfo

# Telegram
TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USERS: list[int] = [
    int(uid) for uid in os.environ.get("ALLOWED_USERS", "").split(",") if uid.strip()
]

# Display (reTerminal E1001: 7.5" mono, 800x480)
DISPLAY_WIDTH: int = 800
DISPLAY_HEIGHT: int = 480
RECIPE_HEIGHT: int = DISPLAY_HEIGHT  # full panel — renderer owns every row

# API
API_HOST: str = os.environ.get("API_HOST", "0.0.0.0")
API_PORT: int = int(os.environ.get("API_PORT", "8080"))
API_KEY: str = os.environ.get("API_KEY", "").strip()
if not API_KEY:
    raise RuntimeError(
        "API_KEY env var is required and must not be empty. "
        "Generate one with: "
        "python3 -c \"import secrets; print(secrets.token_urlsafe(32))\""
    )

# Public web app URL (optional). When set, the bot's /start and /help
# include a clickable link to the web library; otherwise the path is
# described in plain text. Trailing slashes are trimmed.
WEB_URL: str = os.environ.get("WEB_URL", "").rstrip("/")

# Data directory (docker volume)
DATA_DIR: str = os.environ.get("DATA_DIR", "/app/data")

# Local timezone. Drives the anniversary midnight tick and the saved_at
# MM-DD comparison so they line up with the user's wall clock across DST.
TZ_NAME: str = os.environ.get("TZ", "Europe/Zurich")
TZ: ZoneInfo = ZoneInfo(TZ_NAME)

# Target wall-clock hour (local time) for the e-ink panel's daily wake.
# On every successful /version query, the firmware aligns its next
# timer-driven wake to the next occurrence of this hour, so the panel
# is ready when you walk into the kitchen instead of drifting via a
# flat 24-h offset from the last button press. 0..23. Default 6 (06:00).
DEVICE_WAKE_HOUR_LOCAL: int = int(os.environ.get("DEVICE_WAKE_HOUR_LOCAL", "6"))
if not 0 <= DEVICE_WAKE_HOUR_LOCAL <= 23:
    raise RuntimeError(
        f"DEVICE_WAKE_HOUR_LOCAL must be 0..23, got {DEVICE_WAKE_HOUR_LOCAL}"
    )

# Backup — when BACKUP_CHAT_ID is set, the midnight scheduler tick
# uploads a gzipped DB snapshot to that chat *if the library has
# changed since the previous upload*. Quiet days produce no message.
BACKUP_CHAT_ID: int | None = (
    int(os.environ["BACKUP_CHAT_ID"]) if os.environ.get("BACKUP_CHAT_ID") else None
)

# LLM (Infomaniak AI Tools or any OpenAI-compatible endpoint).
# When LLM_API_URL + LLM_API_KEY are both set, the URL flow grows an
# LLM fallback (used when recipe-scrapers fails and no embedded JSON-LD
# is present), and photo uploads are OCR'd into recipes. When unset,
# those paths degrade gracefully — URL fallback skipped, photos rejected
# with a clear error.
#
# LLM_API_URL is the OpenAI base — e.g.
#   https://api.infomaniak.com/2/ai/<product_id>/openai/v1
# The client appends `/chat/completions`.
LLM_API_URL: str = os.environ.get("LLM_API_URL", "").rstrip("/")
LLM_API_KEY: str = os.environ.get("LLM_API_KEY", "").strip()
# Default both paths to Ministral-3-14B (Infomaniak): 14B / multilingual
# (FR/DE/IT/EN) / Image-Text to Text, follows the "name the ingredient"
# prompt rule that gemma3n ignores. Override via env per-deployment.
LLM_TEXT_MODEL: str = os.environ.get("LLM_TEXT_MODEL", "mistralai/Ministral-3-14B-Instruct-2512")
LLM_VISION_MODEL: str = os.environ.get("LLM_VISION_MODEL", "mistralai/Ministral-3-14B-Instruct-2512")
# Translation (recipe → bilingual FTS keywords) is narrower than extraction
# but still requires real bilingual competence: gemma3n empirically leaves
# half the source-language words untranslated and hallucinates fillers
# (translated "quatre-épices" as "Welpe" / puppy). Same model as the
# extraction path keeps quality consistent at trivial extra cost.
# Falls back to LLM_TEXT_MODEL if explicitly cleared.
LLM_TRANSLATE_MODEL: str = (
    os.environ.get("LLM_TRANSLATE_MODEL", "mistralai/Ministral-3-14B-Instruct-2512").strip()
    or LLM_TEXT_MODEL
)


# Fonts (DejaVu Sans, installed via apt in Docker)
FONT_DIR: str = "/usr/share/fonts/truetype/dejavu"
FONT_REGULAR: str = os.path.join(FONT_DIR, "DejaVuSans.ttf")
FONT_BOLD: str = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")

# Layout
MARGIN: int = 20
COLUMN_GAP: int = 16
INGREDIENTS_WIDTH_RATIO: float = 0.35
