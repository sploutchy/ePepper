"""ePepper configuration — all from env vars."""

import os

# Telegram
TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USERS: list[int] = [
    int(uid) for uid in os.environ.get("ALLOWED_USERS", "").split(",") if uid.strip()
]

# Display (reTerminal E1001: 7.5" mono, 800x480)
DISPLAY_WIDTH: int = 800
DISPLAY_HEIGHT: int = 480
RECIPE_HEIGHT: int = DISPLAY_HEIGHT  # full panel; ESP32 overlays clock in the footer strip

# API
API_HOST: str = os.environ.get("API_HOST", "0.0.0.0")
API_PORT: int = int(os.environ.get("API_PORT", "8080"))

# Data directory (docker volume)
DATA_DIR: str = os.environ.get("DATA_DIR", "/app/data")

# Fonts (DejaVu Sans, installed via apt in Docker)
FONT_DIR: str = "/usr/share/fonts/truetype/dejavu"
FONT_REGULAR: str = os.path.join(FONT_DIR, "DejaVuSans.ttf")
FONT_BOLD: str = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")

# Layout
MARGIN: int = 20
COLUMN_GAP: int = 16
INGREDIENTS_WIDTH_RATIO: float = 0.35
