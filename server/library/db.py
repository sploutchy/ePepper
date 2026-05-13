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
import sqlite3
import time
from typing import Any

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
"""


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
    log.info("Library DB ready at %s", DB_PATH)


def upsert_recipe(url: str, recipe: dict) -> int:
    """Insert or update a recipe by URL. Returns the recipe id.

    Always overwrites title / parsed_json / lang so a re-fetched recipe
    picks up corrected parsing. Leaves rating / saved_at untouched.
    """
    now = int(time.time())
    payload = json.dumps(recipe, ensure_ascii=False)
    title = recipe.get("title") or "Untitled"
    lang = recipe.get("lang") or "en"

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
            (url, title, payload, lang, now),
        )
        row = cur.fetchone()
    return int(row["id"])


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
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, url, title, parsed_json, lang, rating, saved_at, created_at "
            "FROM recipes WHERE url = ?",
            (url,),
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
    return int(cur.lastrowid)


def get_comments(recipe_id: int) -> list[dict[str, Any]]:
    """Return comments for a recipe, oldest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, body, created_at FROM comments "
            "WHERE recipe_id = ? ORDER BY created_at ASC",
            (recipe_id,),
        ).fetchall()
    return [{"id": r["id"], "body": r["body"], "created_at": r["created_at"]} for r in rows]
