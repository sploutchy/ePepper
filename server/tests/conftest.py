"""Test bootstrap: config.py reads env vars at import time and raises on
missing required ones, so stub them BEFORE any module under test is
imported. DATA_DIR points at a per-session temp dir so library/db tests
never touch a real database.
"""

import os
import sys
import tempfile

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("API_KEY", "test-api-key-0123456789")
os.environ.setdefault("TZ", "Europe/Zurich")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="epepper-tests-"))

# Make `import library`, `import config`, … resolve to the server modules
# regardless of where pytest is invoked from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
