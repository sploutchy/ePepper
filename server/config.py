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

# Data directory (docker volume)
DATA_DIR: str = os.environ.get("DATA_DIR", "/app/data")

# Local timezone. Drives the anniversary midnight tick and the saved_at
# MM-DD comparison so they line up with the user's wall clock across DST.
TZ_NAME: str = os.environ.get("TZ", "Europe/Zurich")
TZ: ZoneInfo = ZoneInfo(TZ_NAME)

# Backup — when BACKUP_CHAT_ID is set, library mutations trigger a gzipped
# DB snapshot sent to that chat. Rapid mutations are coalesced so a
# /save+/rate+/comment burst yields one upload.
BACKUP_CHAT_ID: int | None = (
    int(os.environ["BACKUP_CHAT_ID"]) if os.environ.get("BACKUP_CHAT_ID") else None
)
BACKUP_DEBOUNCE_S: int = int(os.environ.get("BACKUP_DEBOUNCE_S", "60"))

# Fonts (DejaVu Sans, installed via apt in Docker)
FONT_DIR: str = "/usr/share/fonts/truetype/dejavu"
FONT_REGULAR: str = os.path.join(FONT_DIR, "DejaVuSans.ttf")
FONT_BOLD: str = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")

# Layout
MARGIN: int = 20
COLUMN_GAP: int = 16
INGREDIENTS_WIDTH_RATIO: float = 0.35
