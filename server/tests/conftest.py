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

import pytest
from fastapi.testclient import TestClient

from library import db


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    """Fresh, isolated SQLite DB per test — separate from the shared
    session-wide DATA_DIR above (used as-is by tests that don't need
    per-test isolation, e.g. test_library_db.py's setup_module + unique
    URLs). monkeypatch restores the session DB_PATH after each test."""
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "recipes.db"))
    db.init_db()
    return db


# The session cookie is Secure, so a client on http:// would accept it and
# then never send it back. https matches the real deployment (Secure means
# /app/ has to be served over TLS anyway) and lets login/logout round-trip.
_BASE_URL = "https://testserver"


@pytest.fixture
def client(test_db):
    """Authenticated TestClient for the /app web routes — admin (API_KEY)."""
    from api.server import app
    from api.web import COOKIE_NAME, session_cookie_value

    c = TestClient(app, base_url=_BASE_URL)
    c.cookies.set(COOKIE_NAME, session_cookie_value())
    return c


@pytest.fixture
def anon_client(test_db):
    """Unauthenticated TestClient — for auth-gate tests."""
    from api.server import app

    return TestClient(app, base_url=_BASE_URL)


@pytest.fixture
def make_client(test_db, monkeypatch):
    """Factory for a TestClient signed in as a non-admin access code.

    Installs the principal into `config.PRINCIPALS` (which `api/web.py`
    reads at request time, not import time) and sets the matching cookie.

        client = make_client(grants="viewer")
        client = make_client(grants="library.view|display.push")
    """
    def _make(label="guests", grants="viewer", code="test-access-code-9876"):
        import authz
        import config
        from api.server import app
        from api.web import COOKIE_NAME, session_cookie_value

        principal = authz.parse_access_codes(f"{label}:{code}:{grants}")[0]
        monkeypatch.setattr(
            config, "PRINCIPALS",
            [authz.admin_principal(config.API_KEY), principal],
        )
        c = TestClient(app, base_url=_BASE_URL)
        c.cookies.set(COOKIE_NAME, session_cookie_value(principal))
        return c

    return _make


@pytest.fixture
def viewer_client(make_client):
    """The requested read-only code: the repertoire and nothing else."""
    return make_client(grants="viewer")
