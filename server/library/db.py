"""SQLite-backed recipe library.

Schema:
    recipes  (id, url, title, parsed_json, lang, rating, saved_at,
              last_displayed_at, displayed_count, created_at,
              deleted_at, source)
    comments (id, recipe_id, body, created_at)
    sessions (token_hash, created_at, expires_at)

`saved_at` is NULL until the user explicitly saves the recipe via the
inline button; until then the row is just a cached copy of the parsed
recipe so we can re-render it on demand. `last_displayed_at` is bumped
every time a saved row gets pushed to the panel (see `touch_displayed`),
and drives the "recently shown" sort + the anniversary scheduler.
`displayed_count` is incremented alongside it so the library can show
"cooked N×" and offer a "most cooked" sort. `created_at` is set on
first upsert.

All timestamps are Unix seconds (integer).
"""

import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from typing import Any
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from config import DATA_DIR

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "recipes.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    url               TEXT NOT NULL UNIQUE,
    title             TEXT NOT NULL,
    parsed_json       TEXT NOT NULL,
    lang              TEXT NOT NULL,
    rating            INTEGER,
    saved_at          INTEGER,
    created_at        INTEGER NOT NULL,
    deleted_at        INTEGER,
    source            TEXT,          -- lowercase source name (from URL); NULL if unsourced
    last_displayed_at INTEGER,       -- updated on every successful push_recipe_to_display
    displayed_count   INTEGER NOT NULL DEFAULT 0  -- incremented alongside last_displayed_at
);

CREATE INDEX IF NOT EXISTS idx_recipes_saved_at ON recipes(saved_at);
-- idx_recipes_source + idx_recipes_last_displayed_at live inside their
-- respective _migrate_* helpers so they can't race the ALTER TABLE on a
-- DB that pre-dated either column.

CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id  INTEGER NOT NULL,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id);

-- Web-app session tokens. The token itself is never stored; only its
-- sha256 hash, so a DB read alone can't impersonate a session.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);

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
        _migrate_add_deleted_at(conn)
        _migrate_add_source(conn)
        _migrate_add_last_displayed(conn)
        _migrate_add_displayed_count(conn)
        _migrate_normalize_urls(conn)
        _rebuild_fts(conn)
    log.info("Library DB ready at %s", DB_PATH)


def _migrate_add_deleted_at(conn: sqlite3.Connection) -> None:
    """Add `deleted_at` to an older DB if it's missing. Idempotent."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(recipes)").fetchall()}
    if "deleted_at" not in cols:
        conn.execute("ALTER TABLE recipes ADD COLUMN deleted_at INTEGER")
        log.info("Migration: added recipes.deleted_at column")


def _migrate_add_source(conn: sqlite3.Connection) -> None:
    """Add `source` column and backfill it from existing URLs. Idempotent."""
    from status_helpers import source_name
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(recipes)").fetchall()}
    if "source" not in cols:
        conn.execute("ALTER TABLE recipes ADD COLUMN source TEXT")
        log.info("Migration: added recipes.source column")
    # Index lives here (not in _SCHEMA) so it can't race the ALTER above on
    # a DB that pre-dated the column.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_recipes_source ON recipes(source)")
    # Backfill anywhere the column is still NULL (covers both fresh-add
    # and any rows inserted before this migration landed).
    rows = conn.execute(
        "SELECT id, url FROM recipes WHERE source IS NULL"
    ).fetchall()
    updated = 0
    for row in rows:
        src = source_name(row["url"])
        if src:
            conn.execute(
                "UPDATE recipes SET source = ? WHERE id = ?",
                (src.lower(), row["id"]),
            )
            updated += 1
    if updated:
        log.info("Migration: backfilled source on %d rows", updated)


def _migrate_add_last_displayed(conn: sqlite3.Connection) -> None:
    """Add `last_displayed_at`. Idempotent — no backfill.

    Existing rows stay NULL ("never shown") until a real push bumps them
    via `touch_displayed`. Callers must treat NULL as a first-class state
    rather than a date.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(recipes)").fetchall()}
    if "last_displayed_at" not in cols:
        conn.execute("ALTER TABLE recipes ADD COLUMN last_displayed_at INTEGER")
        log.info("Migration: added recipes.last_displayed_at column")
    # Index lives here (not in _SCHEMA) so it can't race the ALTER above on
    # a DB that pre-dated the column.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recipes_last_displayed_at "
        "ON recipes(last_displayed_at)"
    )


def _migrate_add_displayed_count(conn: sqlite3.Connection) -> None:
    """Add `displayed_count` defaulting to 0. Idempotent.

    SQLite's ALTER TABLE ADD COLUMN propagates the literal DEFAULT to
    existing rows, so no manual backfill is needed. Incremented alongside
    `last_displayed_at` by `touch_displayed`.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(recipes)").fetchall()}
    if "displayed_count" not in cols:
        conn.execute(
            "ALTER TABLE recipes ADD COLUMN displayed_count INTEGER NOT NULL DEFAULT 0"
        )
        log.info("Migration: added recipes.displayed_count column")


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
    picks up corrected parsing. Leaves rating / saved_at untouched. Clears
    `deleted_at` on conflict — re-pushing a URL is an explicit user signal
    that they want the recipe back (caller would also crash on the get_recipe
    that follows, since that filter excludes soft-deleted rows).
    """
    from status_helpers import source_name
    now = int(time.time())
    payload = json.dumps(recipe, ensure_ascii=False)
    title = recipe.get("title") or "Untitled"
    lang = recipe.get("lang") or "en"
    canonical = normalize_url(url)
    ingredients = _ingredients_text(payload)
    src = source_name(canonical)
    source_lower = src.lower() if src else None

    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO recipes (url, title, parsed_json, lang, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title       = excluded.title,
                parsed_json = excluded.parsed_json,
                lang        = excluded.lang,
                source      = excluded.source,
                deleted_at  = NULL
            RETURNING id
            """,
            (canonical, title, payload, lang, source_lower, now),
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
        "last_displayed_at": row["last_displayed_at"],
        "displayed_count": row["displayed_count"],
        "created_at": row["created_at"],
    }


def get_recipe(recipe_id: int) -> dict | None:
    """Fetch a non-deleted recipe row + parsed dict. None if not found."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, url, title, parsed_json, lang, rating, saved_at, last_displayed_at, displayed_count, created_at "
            "FROM recipes WHERE id = ? AND deleted_at IS NULL",
            (recipe_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def find_by_url(url: str) -> dict | None:
    """Return the saved recipe for a URL, or None if we don't have it or it's deleted."""
    canonical = normalize_url(url)
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, url, title, parsed_json, lang, rating, saved_at, last_displayed_at, displayed_count, created_at "
            "FROM recipes WHERE url = ? AND deleted_at IS NULL",
            (canonical,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def mark_saved(recipe_id: int, rating: int) -> bool:
    """Set rating + saved_at on a recipe. Returns True if the row was updated.

    Returns False on either an out-of-range rating or a missing row, matching
    the "rows-affected" convention used by the rest of the module.

    Also clears `deleted_at` — re-saving a previously-deleted URL restores
    the row, matching user intent (the Save button is the only way to keep
    something in the library). Doesn't touch `last_displayed_at`: rating a
    recipe isn't displaying it. A row that has never been pushed (e.g.
    rated via the web widget without a push) stays "never shown" until a
    real display.
    """
    if not 1 <= rating <= 5:
        return False
    now = int(time.time())
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE recipes "
            "SET rating = ?, saved_at = COALESCE(saved_at, ?), deleted_at = NULL "
            "WHERE id = ?",
            (rating, now, recipe_id),
        )
    return cur.rowcount > 0


def touch_displayed(recipe_id: int) -> None:
    """Mark a library row as displayed *right now*.

    Called after every successful `push_recipe_to_display`. Bumps both
    `last_displayed_at` (used by the "recently shown" sort and the
    anniversary scheduler) and `displayed_count` (drives the "cooked N×"
    badge and the "most cooked" sort). No-op if the row doesn't exist or
    is soft-deleted — pushing a deleted row shouldn't revive it, and
    pushing a row that vanished isn't worth complaining about.
    """
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            "UPDATE recipes "
            "SET last_displayed_at = ?, displayed_count = displayed_count + 1 "
            "WHERE id = ? AND deleted_at IS NULL",
            (now, recipe_id),
        )


def delete_recipe(recipe_id: int) -> bool:
    """Soft-delete a recipe. Returns True if a non-deleted row was hit."""
    now = int(time.time())
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE recipes SET deleted_at = ? "
            "WHERE id = ? AND deleted_at IS NULL",
            (now, recipe_id),
        )
        affected = cur.rowcount > 0
        if affected:
            # Drop the row from the FTS index so search results stop returning it.
            conn.execute("DELETE FROM recipes_fts WHERE rowid = ?", (recipe_id,))
    return affected


def remove_comment(comment_id: int) -> int | None:
    """Delete a comment. Returns the parent recipe_id if a row was deleted, else None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT recipe_id FROM comments WHERE id = ?", (comment_id,)
        ).fetchone()
        if row is None:
            return None
        recipe_id = int(row["recipe_id"])
        conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
        # Re-index FTS with the new (shorter) notes blob.
        recipe = conn.execute(
            "SELECT title, parsed_json FROM recipes WHERE id = ?", (recipe_id,)
        ).fetchone()
        if recipe is not None:
            _fts_upsert(
                conn,
                recipe_id,
                recipe["title"],
                _ingredients_text(recipe["parsed_json"]),
                _comments_text(conn, recipe_id),
            )
    return recipe_id


def add_comment(recipe_id: int, body: str) -> int | None:
    """Append a comment to a recipe. Returns the comment id, or None if the
    recipe doesn't exist or has been soft-deleted (so callers can't end up
    with orphan comments on a deleted row)."""
    now = int(time.time())
    with _connect() as conn:
        row = conn.execute(
            "SELECT title, parsed_json FROM recipes "
            "WHERE id = ? AND deleted_at IS NULL",
            (recipe_id,),
        ).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "INSERT INTO comments (recipe_id, body, created_at) VALUES (?, ?, ?)",
            (recipe_id, body, now),
        )
        comment_id = int(cur.lastrowid)
        _fts_upsert(
            conn,
            recipe_id,
            row["title"],
            _ingredients_text(row["parsed_json"]),
            _comments_text(conn, recipe_id),
        )
    return comment_id


def count_saved() -> int:
    """Number of non-deleted recipes in the library that the user explicitly saved."""
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM recipes "
            "WHERE saved_at IS NOT NULL AND deleted_at IS NULL"
        ).fetchone()[0]


def pick_anniversary_recipe(today_mmdd: str, today_year: int) -> dict | None:
    """Return the most recently-displayed saved recipe whose
    `last_displayed_at` (local time) lands on the calendar day `today_mmdd`
    (format 'MM-DD') in a year strictly before `today_year`. None if no
    past-year match exists.

    "Strictly before today_year" prevents replaying a recipe shown earlier
    today. Keying off `last_displayed_at` (instead of `saved_at`) means the
    anniversary tracks your actual cooking cadence: a recipe you displayed
    on 2025-05-20 resurfaces on 2026-05-20, regardless of when it was first
    saved.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, url, title, parsed_json, lang, rating, saved_at, last_displayed_at, displayed_count, created_at
            FROM recipes
            WHERE saved_at IS NOT NULL
              AND deleted_at IS NULL
              AND last_displayed_at IS NOT NULL
              AND strftime('%m-%d', last_displayed_at, 'unixepoch', 'localtime') = ?
              AND CAST(strftime('%Y', last_displayed_at, 'unixepoch', 'localtime') AS INTEGER) < ?
            ORDER BY last_displayed_at DESC
            LIMIT 1
            """,
            (today_mmdd, today_year),
        ).fetchone()
    return _row_to_dict(row) if row else None


# Whitelisted ORDER BY snippets keyed by the `sort` param. Values are
# spliced into SQL, so the dict is the trust boundary — never accept
# free-form sort strings. "recent" / "oldest" key off `last_displayed_at`
# so the library surfaces "what have I cooked lately?" instead of "when
# did I first save this?". NULL `last_displayed_at` = "never shown":
#   - "recent" puts never-shown at the bottom (nothing recent to surface)
#   - "oldest" puts never-shown at the top (nothing's more stale than that)
# `saved_at` is the secondary tie-break so deploy-day libraries (all-NULL
# `last_displayed_at`) match the prior newest-saved / oldest-saved ordering.
_SORT_ORDERS: dict[str, str] = {
    "rated": "r.rating DESC NULLS LAST, r.last_displayed_at DESC NULLS LAST, r.saved_at DESC",
    "rated_low": "r.rating ASC NULLS LAST, r.last_displayed_at DESC NULLS LAST, r.saved_at DESC",
    "oldest": "r.last_displayed_at ASC NULLS FIRST, r.saved_at ASC",
    "recent": "r.last_displayed_at DESC NULLS LAST, r.saved_at DESC",
    "most_cooked": "r.displayed_count DESC, r.last_displayed_at DESC NULLS LAST, r.saved_at DESC",
}


def list_recipes(
    offset: int = 0,
    limit: int = 20,
    query: str | None = None,
    sort: str | None = None,
    min_rating: int | None = None,
    source: str | None = None,
) -> list[dict]:
    """Paginated list of saved, non-deleted recipes.

    sort: one of the keys in _SORT_ORDERS, or None. When None and a
    query is given, results come from FTS5 ordered by relevance; with no
    query and no sort, newest-saved first.

    min_rating: hides recipes rated below this value (1–5). NULL ratings
    are always filtered out when min_rating is set, since "≥ N" is
    meaningless for an unrated row.

    source: lowercase source key (matching `source_name(url).lower()`).
    Filters to recipes whose stored `source` column equals this value.
    """
    order_by = _SORT_ORDERS.get(sort) if sort else None
    where_extra = ""
    extra_params: list = []
    if min_rating is not None and 1 <= min_rating <= 5:
        where_extra += " AND r.rating >= ? "
        extra_params.append(min_rating)
    if source:
        where_extra += " AND r.source = ? "
        extra_params.append(source.lower())

    if query and query.strip():
        tokens = _FTS_TOKEN_RE.findall(query)
        if not tokens:
            return []
        fts_query = " ".join(f'"{t}"' for t in tokens)
        order_sql = order_by or "rank"
        with _connect() as conn:
            rows = conn.execute(
                f"""
                SELECT r.id, r.url, r.title, r.parsed_json, r.lang, r.rating,
                       r.saved_at, r.last_displayed_at, r.displayed_count, r.created_at
                FROM recipes_fts f
                JOIN recipes r ON r.id = f.rowid
                WHERE recipes_fts MATCH ?
                  AND r.saved_at IS NOT NULL
                  AND r.deleted_at IS NULL
                  {where_extra}
                ORDER BY {order_sql}
                LIMIT ? OFFSET ?
                """,
                (fts_query, *extra_params, limit, offset),
            ).fetchall()
    else:
        order_sql = order_by or "r.last_displayed_at DESC NULLS LAST, r.saved_at DESC"
        with _connect() as conn:
            rows = conn.execute(
                f"""
                SELECT r.id, r.url, r.title, r.parsed_json, r.lang, r.rating,
                       r.saved_at, r.last_displayed_at, r.displayed_count, r.created_at
                FROM recipes r
                WHERE r.saved_at IS NOT NULL AND r.deleted_at IS NULL
                  {where_extra}
                ORDER BY {order_sql}
                LIMIT ? OFFSET ?
                """,
                (*extra_params, limit, offset),
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def random_recipe() -> dict | None:
    """Return one randomly-picked saved, non-deleted recipe, or None if
    the library is empty. Used by the bot's /surprise command."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, url, title, parsed_json, lang, rating, saved_at, last_displayed_at, displayed_count, created_at "
            "FROM recipes "
            "WHERE saved_at IS NOT NULL AND deleted_at IS NULL "
            "ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_sources() -> list[str]:
    """Distinct lowercase source keys from saved, non-deleted recipes,
    sorted alphabetically. Used to populate the library page's source
    filter dropdown."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT source FROM recipes "
            "WHERE source IS NOT NULL "
            "  AND saved_at IS NOT NULL "
            "  AND deleted_at IS NULL "
            "ORDER BY source"
        ).fetchall()
    return [r["source"] for r in rows]


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
                   r.saved_at, r.last_displayed_at, r.displayed_count, r.created_at
            FROM recipes_fts f
            JOIN recipes r ON r.id = f.rowid
            WHERE recipes_fts MATCH ?
              AND r.saved_at IS NOT NULL
              AND r.deleted_at IS NULL
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


# --- Web-app sessions ------------------------------------------------------

# Sliding window: a session that's actively used gets renewed for another
# 30 days on each request; one untouched for 30 days expires.
SESSION_DURATION_S = 30 * 24 * 3600
# Only rewrite the expiry when it's drifted by more than a day, to avoid
# a write on every single request.
_SESSION_SLIDE_THRESHOLD_S = 24 * 3600


def _hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session() -> str:
    """Mint a new session token (returned in plaintext) and store its hash."""
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, created_at, expires_at) VALUES (?, ?, ?)",
            (_hash_session_token(token), now, now + SESSION_DURATION_S),
        )
    return token


def validate_session(token: str) -> bool:
    """Return True iff the token matches a non-expired session row. Slides
    the expiry forward on a valid hit and opportunistically GCs expired rows."""
    if not token:
        return False
    h = _hash_session_token(token)
    now = int(time.time())
    with _connect() as conn:
        row = conn.execute(
            "SELECT expires_at FROM sessions WHERE token_hash = ?",
            (h,),
        ).fetchone()
        if row is None or row["expires_at"] < now:
            return False
        new_expires = now + SESSION_DURATION_S
        if new_expires - row["expires_at"] > _SESSION_SLIDE_THRESHOLD_S:
            conn.execute(
                "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                (new_expires, h),
            )
        # Drive-by cleanup of stale rows. Cheap thanks to idx_sessions_expires_at.
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    return True


def delete_session(token: str) -> None:
    """Invalidate a session (logout). No-op if the token isn't known."""
    if not token:
        return
    with _connect() as conn:
        conn.execute(
            "DELETE FROM sessions WHERE token_hash = ?",
            (_hash_session_token(token),),
        )
