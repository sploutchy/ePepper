"""SQLite-backed recipe library.

Schema:
    recipes (id, url, title, parsed_json, lang, rating, saved_at, created_at)
    comments (id, recipe_id, body, created_at)

`saved_at` is NULL until the user explicitly saves the recipe via the
inline button; until then the row is just a cached copy of the parsed
recipe so we can re-render it on demand. `created_at` is set on first
upsert.

All timestamps are Unix seconds (integer).
"""

import json
import logging
import os
import re
import sqlite3
import time
from typing import Any
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from config import DATA_DIR

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "recipes.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    parsed_json TEXT NOT NULL,
    lang        TEXT NOT NULL,
    rating      INTEGER,
    saved_at    INTEGER,
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recipes_saved_at ON recipes(saved_at);

CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id  INTEGER NOT NULL,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id);

-- FTS5 index for /search. rowid mirrors recipes.id so we can JOIN cheaply.
-- We manage inserts/updates/deletes manually rather than via triggers
-- because ingredients and notes are derived (JSON / aggregated comments).
CREATE VIRTUAL TABLE IF NOT EXISTS recipes_fts USING fts5(
    title, ingredients, notes,
    tokenize='unicode61 remove_diacritics 2'
);
"""


_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAM_EXACT = {"gclid", "fbclid", "ref"}


def normalize_url(url: str) -> str:
    """Canonicalize a recipe URL so equivalent forms collide on the UNIQUE index.

    - Lowercase scheme + host
    - Strip fragment (#anchor)
    - Strip trailing slash on path (unless path is just "/")
    - Drop tracking params (utm_*, gclid, fbclid, ref); keep everything else
      so content-affecting params like `menge=60` still differentiate.
    """
    parts = urlparse(url.strip())
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    if parts.query:
        kept = [
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith(_TRACKING_PARAM_PREFIXES)
            and k.lower() not in _TRACKING_PARAM_EXACT
        ]
        query = urlencode(kept)
    else:
        query = ""
    return urlunparse((parts.scheme.lower(), parts.netloc.lower(), path, parts.params, query, ""))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create tables on first run. Safe to call repeatedly."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        _migrate_normalize_urls(conn)
        _rebuild_fts(conn)
    log.info("Library DB ready at %s", DB_PATH)


def _ingredients_text(parsed_json: str) -> str:
    """Flatten a recipe's ingredients list (stored in parsed_json) into a search blob."""
    try:
        data = json.loads(parsed_json)
    except (ValueError, TypeError):
        return ""
    ings = data.get("ingredients") or []
    return "\n".join(str(i) for i in ings if i)


def _comments_text(conn: sqlite3.Connection, recipe_id: int) -> str:
    rows = conn.execute(
        "SELECT body FROM comments WHERE recipe_id = ? ORDER BY created_at ASC",
        (recipe_id,),
    ).fetchall()
    return "\n".join(r["body"] for r in rows)


def _fts_upsert(conn: sqlite3.Connection, recipe_id: int, title: str, ingredients: str, notes: str) -> None:
    conn.execute("DELETE FROM recipes_fts WHERE rowid = ?", (recipe_id,))
    conn.execute(
        "INSERT INTO recipes_fts (rowid, title, ingredients, notes) VALUES (?, ?, ?, ?)",
        (recipe_id, title, ingredients, notes),
    )


def _rebuild_fts(conn: sqlite3.Connection) -> None:
    """Re-populate `recipes_fts` from `recipes` + `comments`. Idempotent."""
    conn.execute("DELETE FROM recipes_fts")
    rows = conn.execute("SELECT id, title, parsed_json FROM recipes").fetchall()
    for row in rows:
        _fts_upsert(
            conn,
            row["id"],
            row["title"],
            _ingredients_text(row["parsed_json"]),
            _comments_text(conn, row["id"]),
        )


def _migrate_normalize_urls(conn: sqlite3.Connection) -> None:
    """Rewrite any pre-normalization URLs in `recipes.url` to canonical form.

    Idempotent — rows already canonical are no-ops. On a normalization
    collision (two old rows that canonicalize to the same URL) we keep the
    earlier row and drop the duplicate's row; comments referencing it get
    cleaned up via the FK cascade.
    """
    rows = conn.execute("SELECT id, url FROM recipes ORDER BY id ASC").fetchall()
    for row in rows:
        canonical = normalize_url(row["url"])
        if canonical == row["url"]:
            continue
        clash = conn.execute(
            "SELECT id FROM recipes WHERE url = ? AND id != ?",
            (canonical, row["id"]),
        ).fetchone()
        if clash is not None:
            log.warning(
                "URL normalization collision: dropping recipe id=%d (raw=%r) — "
                "already covered by id=%d",
                row["id"], row["url"], clash["id"],
            )
            conn.execute("DELETE FROM recipes WHERE id = ?", (row["id"],))
        else:
            conn.execute(
                "UPDATE recipes SET url = ? WHERE id = ?", (canonical, row["id"])
            )


def upsert_recipe(url: str, recipe: dict) -> int:
    """Insert or update a recipe by URL. Returns the recipe id.

    Always overwrites title / parsed_json / lang so a re-fetched recipe
    picks up corrected parsing. Leaves rating / saved_at untouched.
    """
    now = int(time.time())
    payload = json.dumps(recipe, ensure_ascii=False)
    title = recipe.get("title") or "Untitled"
    lang = recipe.get("lang") or "en"
    canonical = normalize_url(url)
    ingredients = _ingredients_text(payload)

    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO recipes (url, title, parsed_json, lang, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title       = excluded.title,
                parsed_json = excluded.parsed_json,
                lang        = excluded.lang
            RETURNING id
            """,
            (canonical, title, payload, lang, now),
        )
        recipe_id = int(cur.fetchone()["id"])
        _fts_upsert(conn, recipe_id, title, ingredients, _comments_text(conn, recipe_id))
    return recipe_id


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "url": row["url"],
        "title": row["title"],
        "recipe": json.loads(row["parsed_json"]),
        "lang": row["lang"],
        "rating": row["rating"],
        "saved_at": row["saved_at"],
        "created_at": row["created_at"],
    }


def get_recipe(recipe_id: int) -> dict | None:
    """Fetch a recipe row + parsed dict. Returns None if not found."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, url, title, parsed_json, lang, rating, saved_at, created_at "
            "FROM recipes WHERE id = ?",
            (recipe_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def find_by_url(url: str) -> dict | None:
    """Return the saved recipe for a URL, or None if we don't have it."""
    canonical = normalize_url(url)
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, url, title, parsed_json, lang, rating, saved_at, created_at "
            "FROM recipes WHERE url = ?",
            (canonical,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def mark_saved(recipe_id: int, rating: int) -> bool:
    """Set rating + saved_at on a recipe. Returns True if the row exists."""
    if not 1 <= rating <= 5:
        raise ValueError(f"rating must be 1..5, got {rating}")
    now = int(time.time())
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE recipes SET rating = ?, saved_at = COALESCE(saved_at, ?) "
            "WHERE id = ?",
            (rating, now, recipe_id),
        )
    return cur.rowcount > 0


def add_comment(recipe_id: int, body: str) -> int:
    """Append a comment to a recipe. Returns the comment id."""
    now = int(time.time())
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO comments (recipe_id, body, created_at) VALUES (?, ?, ?)",
            (recipe_id, body, now),
        )
        comment_id = int(cur.lastrowid)
        row = conn.execute(
            "SELECT title, parsed_json FROM recipes WHERE id = ?", (recipe_id,)
        ).fetchone()
        if row is not None:
            _fts_upsert(
                conn,
                recipe_id,
                row["title"],
                _ingredients_text(row["parsed_json"]),
                _comments_text(conn, recipe_id),
            )
    return comment_id


def pick_anniversary_recipe(today_mmdd: str, today_year: int) -> dict | None:
    """Return the most recently-saved recipe whose `saved_at` (local time) lands on
    the calendar day `today_mmdd` (format 'MM-DD') in a year strictly before
    `today_year`. None if no past-year match exists.

    "Strictly before today_year" prevents replaying a recipe saved earlier today.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, url, title, parsed_json, lang, rating, saved_at, created_at
            FROM recipes
            WHERE saved_at IS NOT NULL
              AND strftime('%m-%d', saved_at, 'unixepoch', 'localtime') = ?
              AND CAST(strftime('%Y', saved_at, 'unixepoch', 'localtime') AS INTEGER) < ?
            ORDER BY saved_at DESC
            LIMIT 1
            """,
            (today_mmdd, today_year),
        ).fetchone()
    return _row_to_dict(row) if row else None


_FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def search(query: str, limit: int = 5) -> list[dict]:
    """Full-text search over saved recipes (title + ingredients + comments).

    Only returns recipes the user explicitly saved (rating set). Most relevant
    first. The query is tokenized into quoted phrases so FTS5 operators in user
    input (AND/OR/NOT/quotes) can't break the parser.
    """
    tokens = _FTS_TOKEN_RE.findall(query)
    if not tokens:
        return []
    fts_query = " ".join(f'"{t}"' for t in tokens)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.url, r.title, r.parsed_json, r.lang, r.rating,
                   r.saved_at, r.created_at
            FROM recipes_fts f
            JOIN recipes r ON r.id = f.rowid
            WHERE recipes_fts MATCH ? AND r.saved_at IS NOT NULL
            ORDER BY rank
            LIMIT ?
            """,
            (fts_query, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_comments(recipe_id: int) -> list[dict[str, Any]]:
    """Return comments for a recipe, oldest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, body, created_at FROM comments "
            "WHERE recipe_id = ? ORDER BY created_at ASC",
            (recipe_id,),
        ).fetchall()
    return [{"id": r["id"], "body": r["body"], "created_at": r["created_at"]} for r in rows]
