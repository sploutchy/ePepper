"""SQLite-backed recipe library.

Tables (full column list in `_SCHEMA` below):
    recipes      — one row per known URL
    recipes_fts  — FTS5 index for /search

`saved_at` is NULL until the user explicitly saves the recipe; until then
the row is just a cached copy of the parsed recipe so we can re-render it
on demand. `last_displayed_at` is bumped every time a saved row gets
pushed to the panel (see `touch_displayed`), and drives the "recently
cooked" sort + the anniversary scheduler. `created_at` is set on first upsert.

All timestamps are Unix seconds (integer).
"""

import datetime
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
MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")
_MIGRATION_PREFIX_RE = re.compile(r"^(\d+)")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    url                 TEXT NOT NULL UNIQUE,
    title               TEXT NOT NULL,
    parsed_json         TEXT NOT NULL,
    lang                TEXT NOT NULL,
    saved_at            INTEGER,
    created_at          INTEGER NOT NULL,
    deleted_at          INTEGER,
    source              TEXT,                       -- lowercase source name (from URL); NULL if unsourced
    last_displayed_at   INTEGER,                    -- updated on every successful push_recipe_to_display
    translated_keywords TEXT,                       -- LLM-produced FR/DE search blob; NULL = pending, "" = tried & gave up
    tags                TEXT                        -- comma-separated lowercase tags; NULL = none
);

CREATE INDEX IF NOT EXISTS idx_recipes_saved_at ON recipes(saved_at);
CREATE INDEX IF NOT EXISTS idx_recipes_source ON recipes(source);
CREATE INDEX IF NOT EXISTS idx_recipes_last_displayed_at ON recipes(last_displayed_at);

-- FTS5 index for /search. rowid mirrors recipes.id so we can JOIN cheaply.
-- We manage inserts/updates/deletes manually rather than via triggers
-- because ingredients and translated keywords are derived
-- (JSON / LLM output).
-- `translated` carries LLM-produced FR/DE keywords so a recipe stored
-- in one language is searchable from the other (see processing.recipes
-- :translate_for_search). Empty until the backfill catches it up.
CREATE VIRTUAL TABLE IF NOT EXISTS recipes_fts USING fts5(
    title, ingredients, tags, translated,
    tokenize='unicode61 remove_diacritics 2'
);

-- Singleton-row table tracking what's currently on the e-ink panel, so a
-- container restart can re-render the same recipe + page instead of
-- coming back to an empty display. Only populated for SAVED recipes
-- (recipe_id is enough to re-derive everything — parsed recipe and
-- url all live on the recipes row). Pushes of unsaved recipes don't write
-- here; clearing the panel deletes the row. `id` is locked to 1 so any
-- INSERT/UPDATE collapses onto the same row.
CREATE TABLE IF NOT EXISTS display_panel (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    recipe_id INTEGER NOT NULL,
    page      INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
);

-- Tracks which schema migrations have been applied. Row (0, ...) marks
-- the baseline schema captured by the CREATE TABLE IF NOT EXISTS block
-- above; any further .sql file in library/migrations/ whose numeric
-- prefix exceeds the highest stored `version` gets run + recorded by
-- init_db on startup.
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Free-form key/value scratch space for one-shot bootstrap flags that
-- don't warrant their own table. `fts_rebuilt` is set to '1' the first
-- time init_db rebuilds the FTS index so the expensive O(library) rebuild
-- runs once (or after a manual snapshot restore that clears the flag)
-- instead of on every container start.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
    # WAL lets readers (e.g. the daily sqlite3 .backup) coexist with writers
    # without grabbing a full-database lock. `synchronous=NORMAL` is the
    # recommended pairing — durability across an OS crash drops from "fsync
    # on every commit" to "fsync at checkpoint", which is fine for a recipe
    # library and removes per-write disk waits. Setting these on every
    # connection is cheap (SQLite no-ops if the mode is already set) and
    # keeps the pragmas correct even if the journal file was recreated.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Wait up to 5 s for a competing writer to finish instead of raising
    # SQLITE_BUSY immediately. The bot, web, scheduler, and the worker-thread
    # backup snapshot can all touch the DB; under that rare contention a bare
    # SQLITE_BUSY would surface as a swallowed error and a silently-lost write.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _current_schema_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied migration version, or -1 if none recorded."""
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    if row is None or row["v"] is None:
        return -1
    return int(row["v"])


def _latest_migration_version() -> int:
    """Return the numeric prefix of the highest-numbered migration file, or 0."""
    if not os.path.isdir(MIGRATIONS_DIR):
        return 0
    versions = []
    for fname in os.listdir(MIGRATIONS_DIR):
        if not fname.endswith(".sql"):
            continue
        m = _MIGRATION_PREFIX_RE.match(fname)
        if m:
            versions.append(int(m.group(1)))
    return max(versions) if versions else 0


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Run any migrations/*.sql whose numeric prefix exceeds the stored version.

    Files are sorted by filename; the leading run of digits is the version.
    Each migration runs inside a transaction so a partial apply doesn't
    leave the DB half-migrated. The `schema_version` row is inserted in
    the same transaction as the migration body.
    """
    if not os.path.isdir(MIGRATIONS_DIR):
        return
    files = sorted(f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql"))
    current = _current_schema_version(conn)
    for fname in files:
        m = _MIGRATION_PREFIX_RE.match(fname)
        if not m:
            continue
        version = int(m.group(1))
        if version <= current:
            continue
        path = os.path.join(MIGRATIONS_DIR, fname)
        with open(path, "r", encoding="utf-8") as fh:
            sql = fh.read()
        applied_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        # `with conn` opens an implicit transaction that commits on exit
        # or rolls back on exception — keeps the migration body and the
        # schema_version bump atomic.
        with conn:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, applied_at),
            )
        log.info("Applied migration %s (version %d)", fname, version)


def init_db() -> None:
    """Create tables on first run. Safe to call repeatedly.

    Three-phase startup:
      1. Run the baseline CREATE TABLE IF NOT EXISTS script. This is
         idempotent and covers every schema element.
      2. Stamp `schema_version` with the latest migration version if no row
         exists yet — brand-new DBs skip all historical migrations since their
         schema is already current. Pre-migrations-era DBs with no recorded
         version start at version 0 and get caught up by `_apply_migrations`.
      3. Apply any library/migrations/*.sql whose numeric prefix is
         greater than the currently-recorded version, recording each as
         it goes. See `_apply_migrations`.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        if _current_schema_version(conn) < 0:
            applied_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            # Stamp at the latest migration version so fresh DBs (whose
            # schema already reflects all migrations) don't re-run historical
            # ALTER/DROP statements against a schema that never had those
            # columns/tables. Pre-existing DBs without any recorded version
            # stamp at 0 instead — that path no longer applies since the
            # migration system was introduced before the first ALTER migration.
            latest = _latest_migration_version()
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (latest, applied_at),
            )
        _apply_migrations(conn)
        # Re-apply the schema after migrations so any tables a migration
        # dropped (e.g. recipes_fts) are recreated before the FTS rebuild
        # below. All statements use IF NOT EXISTS, so this is idempotent.
        conn.executescript(_SCHEMA)
        # One-shot FTS rebuild, gated behind a `meta.fts_rebuilt` sentinel.
        # `_rebuild_fts` derives the index from recipe JSON ingredients + tags
        # + LLM-translated keywords, so it can't be expressed as an FTS5
        # `'rebuild'` command (that only mirrors a single external-content
        # table 1:1). Gating it means the O(library-size) rebuild runs once
        # on a fresh DB and never again on routine restarts. The ongoing index
        # is kept correct incrementally by `_fts_upsert` on every write. To
        # force a rebuild after restoring an old snapshot, delete the
        # `fts_rebuilt` row from `meta`.
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'fts_rebuilt'"
        ).fetchone()
        if row is None:
            _rebuild_fts(conn)
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('fts_rebuilt', '1')"
            )
    log.info("Library DB ready at %s", DB_PATH)


def _ingredients_text(parsed_json: str) -> str:
    """Flatten a recipe's ingredients list (stored in parsed_json) into a search blob."""
    try:
        data = json.loads(parsed_json)
    except (ValueError, TypeError):
        return ""
    ings = data.get("ingredients") or []
    return "\n".join(str(i) for i in ings if i)


def _fts_upsert(
    conn: sqlite3.Connection,
    recipe_id: int,
    title: str,
    ingredients: str,
    tags: str,
    translated: str = "",
) -> None:
    conn.execute("DELETE FROM recipes_fts WHERE rowid = ?", (recipe_id,))
    conn.execute(
        "INSERT INTO recipes_fts (rowid, title, ingredients, tags, translated) "
        "VALUES (?, ?, ?, ?, ?)",
        (recipe_id, title, ingredients, tags, translated),
    )


def _rebuild_fts(conn: sqlite3.Connection) -> None:
    """Re-populate `recipes_fts` from `recipes`. Idempotent."""
    conn.execute("DELETE FROM recipes_fts")
    rows = conn.execute(
        "SELECT id, title, parsed_json, tags, translated_keywords FROM recipes"
    ).fetchall()
    for row in rows:
        _fts_upsert(
            conn,
            row["id"],
            row["title"],
            _ingredients_text(row["parsed_json"]),
            row["tags"] or "",
            row["translated_keywords"] or "",
        )


def upsert_recipe(
    url: str,
    recipe: dict,
    translated_keywords: str | None = None,
    source: str | None = None,
) -> int:
    """Insert or update a recipe by URL. Returns the recipe id.

    Always overwrites title / parsed_json / lang so a re-fetched recipe
    picks up corrected parsing. Leaves saved_at / last_displayed_at
    untouched. Clears
    `deleted_at` on conflict — re-pushing a URL is an explicit user signal
    that they want the recipe back (caller would also crash on the get_recipe
    that follows, since that filter excludes soft-deleted rows).

    `translated_keywords` is the LLM-produced FR/DE search blob from
    `processing.recipes.translate_for_search`. Pass None to leave the
    existing value untouched (an upsert that re-fetches a recipe doesn't
    invalidate the translation). The startup backfill task fills NULLs
    out-of-band.

    `source` is the humanized source name derived from the URL by
    `status_helpers.source_name`; the caller computes it so this module
    stays below `status_helpers` in the import layering. None / "" is
    persisted as NULL (matches the historical behaviour for unsourced
    rows). Stored lowercased for case-insensitive grouping in the
    library filters.
    """
    # Lazy import (processing → library cycle): processing/recipes.py
    # imports library.upsert_recipe, and we import its normalizer here
    # for defence-in-depth. Callers normally validate via
    # processing.recipes.validate_llm_recipe (which also normalizes), so
    # this call is redundant on the happy path but protects test / admin
    # paths that bypass the validator.
    from processing.recipes import normalize_recipe_for_render
    recipe = normalize_recipe_for_render(recipe)
    now = int(time.time())
    payload = json.dumps(recipe, ensure_ascii=False)
    title = recipe.get("title") or "Untitled"
    lang = recipe.get("lang") or "en"
    canonical = normalize_url(url)
    ingredients = _ingredients_text(payload)
    source_lower = source.lower() if source else None

    with _connect() as conn:
        if translated_keywords is None:
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
                RETURNING id, translated_keywords, tags
                """,
                (canonical, title, payload, lang, source_lower, now),
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO recipes
                    (url, title, parsed_json, lang, source, created_at, translated_keywords)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    title              = excluded.title,
                    parsed_json        = excluded.parsed_json,
                    lang               = excluded.lang,
                    source             = excluded.source,
                    deleted_at         = NULL,
                    translated_keywords = excluded.translated_keywords
                RETURNING id, translated_keywords, tags
                """,
                (canonical, title, payload, lang, source_lower, now, translated_keywords),
            )
        row = cur.fetchone()
        recipe_id = int(row["id"])
        translated = row["translated_keywords"] or ""
        tag_str = row["tags"] or ""
        _fts_upsert(conn, recipe_id, title, ingredients, tag_str, translated)
    return recipe_id


def set_translated_keywords(recipe_id: int, blob: str) -> None:
    """Backfill translation for an existing row + re-index its FTS entry.

    Called by the startup backfill task once it has the LLM response. No-op
    if the recipe was deleted between the SELECT and the UPDATE.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT title, parsed_json, tags FROM recipes "
            "WHERE id = ? AND deleted_at IS NULL",
            (recipe_id,),
        ).fetchone()
        if row is None:
            return
        conn.execute(
            "UPDATE recipes SET translated_keywords = ? WHERE id = ?",
            (blob, recipe_id),
        )
        _fts_upsert(
            conn, recipe_id, row["title"], _ingredients_text(row["parsed_json"]),
            row["tags"] or "", blob,
        )


def recipes_needing_translation() -> list[dict]:
    """Return rows whose translated_keywords is NULL.

    Used by the startup backfill. Excludes soft-deleted rows and rows
    that the backfill couldn't translate previously (those got a
    sentinel empty string to mark "tried, gave up") so we don't keep
    pinging the LLM for a row that already failed once.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, title, parsed_json, lang FROM recipes "
            "WHERE translated_keywords IS NULL AND deleted_at IS NULL"
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            out.append({
                "id": int(r["id"]),
                "title": r["title"],
                "recipe": json.loads(r["parsed_json"]),
                "lang": r["lang"],
            })
        return out


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "url": row["url"],
        "title": row["title"],
        "recipe": json.loads(row["parsed_json"]),
        "lang": row["lang"],
        "saved_at": row["saved_at"],
        "last_displayed_at": row["last_displayed_at"],
        "created_at": row["created_at"],
        "tags": [t.strip() for t in (row["tags"] or "").split(",") if t.strip()],
    }


def get_recipe(recipe_id: int) -> dict | None:
    """Fetch a non-deleted recipe row + parsed dict. None if not found."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, url, title, parsed_json, lang, saved_at, last_displayed_at, created_at, tags "
            "FROM recipes WHERE id = ? AND deleted_at IS NULL",
            (recipe_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def find_by_url(url: str) -> dict | None:
    """Return the saved recipe for a URL, or None if we don't have it or it's deleted."""
    canonical = normalize_url(url)
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, url, title, parsed_json, lang, saved_at, last_displayed_at, created_at, tags "
            "FROM recipes WHERE url = ? AND deleted_at IS NULL",
            (canonical,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def save_recipe(recipe_id: int) -> bool:
    """Mark a recipe as saved. Returns True if a row was updated.

    Idempotent — `saved_at` is set via COALESCE, so re-calling on an
    already-saved row doesn't move the original save date. Also clears
    `deleted_at` so re-adding a previously-deleted URL restores the row.
    """
    now = int(time.time())
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE recipes "
            "SET saved_at = COALESCE(saved_at, ?), deleted_at = NULL "
            "WHERE id = ?",
            (now, recipe_id),
        )
    return cur.rowcount > 0


def touch_displayed(recipe_id: int) -> None:
    """Mark a library row as displayed *right now*.

    Called after every successful `push_recipe_to_display`. Bumps
    `last_displayed_at` (used by the "recently shown" sort and the
    anniversary scheduler). No-op if the row doesn't exist or is
    soft-deleted.
    """
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            "UPDATE recipes SET last_displayed_at = ? "
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


def set_tags(recipe_id: int, tags: list[str]) -> bool:
    """Set the tag list for a recipe. Returns True if the row was found and updated."""
    tag_str = ",".join(t.lower().strip() for t in tags if t.strip()) or None
    with _connect() as conn:
        row = conn.execute(
            "SELECT title, parsed_json, translated_keywords FROM recipes "
            "WHERE id = ? AND deleted_at IS NULL",
            (recipe_id,),
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE recipes SET tags = ? WHERE id = ?",
            (tag_str, recipe_id),
        )
        _fts_upsert(
            conn, recipe_id, row["title"], _ingredients_text(row["parsed_json"]),
            tag_str or "", row["translated_keywords"] or "",
        )
    return True


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
            SELECT id, url, title, parsed_json, lang, saved_at, last_displayed_at, created_at, tags
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


def _recipe_ids_with_tag(conn: sqlite3.Connection, tag: str) -> list[int]:
    """Recipe ids whose `tags` column contains `tag` as an exact comma-separated entry."""
    lowered = tag.lower()
    rows = conn.execute(
        """
        SELECT id FROM recipes
        WHERE deleted_at IS NULL
          AND saved_at IS NOT NULL
          AND (
            tags = ?
            OR tags LIKE ?
            OR tags LIKE ?
            OR tags LIKE ?
          )
        """,
        (lowered, f"{lowered},%", f"%,{lowered}", f"%,{lowered},%"),
    ).fetchall()
    return sorted(r["id"] for r in rows)


def list_tags() -> list[tuple[str, int]]:
    """Distinct tags from saved recipes, sorted by frequency desc then alpha.

    Returns `[(tag, count), ...]`. Used to populate the library page's tag
    filter cloud. Tags are stored as comma-separated lowercase strings.
    """
    counts: dict[str, int] = {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tags FROM recipes "
            "WHERE tags IS NOT NULL AND saved_at IS NOT NULL AND deleted_at IS NULL"
        ).fetchall()
    for row in rows:
        for tag in (t.strip() for t in row["tags"].split(",") if t.strip()):
            counts[tag] = counts.get(tag, 0) + 1
    return sorted(counts.items(), key=lambda x: (-x[1], x[0]))


def list_recipes(
    offset: int = 0,
    limit: int = 20,
    query: str | None = None,
    source: str | None = None,
    tag: str | None = None,
) -> list[dict]:
    """Paginated list of saved, non-deleted recipes.

    With a query, results come from FTS5 ordered by relevance; with no
    query, most-recently-cooked first (fixed — no manual sort toggle).

    source: lowercase source key (matching `source_name(url).lower()`).
    Filters to recipes whose stored `source` column equals this value.

    tag: a tag token appearing in the recipe's `tags` column.
    Pre-filtered to a set of ids via a separate query so the main
    pagination can stay in SQL.
    """
    where_extra = ""
    extra_params: list = []
    if source:
        where_extra += " AND r.source = ? "
        extra_params.append(source.lower())
    if tag:
        with _connect() as conn_tag:
            tag_ids = _recipe_ids_with_tag(conn_tag, tag)
        if not tag_ids:
            return []
        placeholders = ",".join("?" for _ in tag_ids)
        where_extra += f" AND r.id IN ({placeholders}) "
        extra_params.extend(tag_ids)

    if query and query.strip():
        tokens = _FTS_TOKEN_RE.findall(query)
        if not tokens:
            return []
        # Append `*` after each quoted token so FTS5 prefix-matches —
        # "kartoffel" finds "kartoffeln", "kartoffelpüree", etc. The
        # quoting still escapes any FTS5 operators the user typed.
        fts_query = " ".join(f'"{t}"*' for t in tokens)
        with _connect() as conn:
            rows = conn.execute(
                f"""
                SELECT r.id, r.url, r.title, r.parsed_json, r.lang,
                       r.saved_at, r.last_displayed_at, r.created_at, r.tags
                FROM recipes_fts f
                JOIN recipes r ON r.id = f.rowid
                WHERE recipes_fts MATCH ?
                  AND r.saved_at IS NOT NULL
                  AND r.deleted_at IS NULL
                  {where_extra}
                ORDER BY rank
                LIMIT ? OFFSET ?
                """,
                (fts_query, *extra_params, limit, offset),
            ).fetchall()
    else:
        # "recent" (most-recently-cooked first) keyed off `last_displayed_at`
        # so the library surfaces "what have I cooked lately?" instead of
        # "when did I first save this?". NULL `last_displayed_at` = "never
        # cooked", sunk to the bottom via NULLS LAST. `saved_at` is the
        # secondary tie-break so deploy-day libraries (all-NULL
        # `last_displayed_at`) match the prior newest-saved ordering.
        with _connect() as conn:
            rows = conn.execute(
                f"""
                SELECT r.id, r.url, r.title, r.parsed_json, r.lang,
                       r.saved_at, r.last_displayed_at, r.created_at, r.tags
                FROM recipes r
                WHERE r.saved_at IS NOT NULL AND r.deleted_at IS NULL
                  {where_extra}
                ORDER BY r.last_displayed_at DESC NULLS LAST, r.saved_at DESC
                LIMIT ? OFFSET ?
                """,
                (*extra_params, limit, offset),
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


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


def search(query: str, limit: int = 5, offset: int = 0) -> list[dict]:
    """Full-text search over saved recipes (title + ingredients + tags).

    Only returns recipes the user explicitly saved (saved_at set). Most
    relevant first. The query is tokenized into quoted phrases so FTS5
    operators in user input (AND/OR/NOT/quotes) can't break the parser.

    `offset` drives bot-side pagination — callers ask for `limit + 1` to
    detect a "more available" tail without a second COUNT query.
    """
    tokens = _FTS_TOKEN_RE.findall(query)
    if not tokens:
        return []
    # Append `*` after each quoted token so FTS5 prefix-matches —
    # "kartoffel" finds "kartoffeln", "kartoffelpüree", etc. The
    # quoting still escapes any FTS5 operators the user typed.
    fts_query = " ".join(f'"{t}"*' for t in tokens)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.url, r.title, r.parsed_json, r.lang,
                   r.saved_at, r.last_displayed_at, r.created_at, r.tags
            FROM recipes_fts f
            JOIN recipes r ON r.id = f.rowid
            WHERE recipes_fts MATCH ?
              AND r.saved_at IS NOT NULL
              AND r.deleted_at IS NULL
            ORDER BY rank
            LIMIT ? OFFSET ?
            """,
            (fts_query, limit, offset),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


# --- Display-panel persistence ---------------------------------------------


def get_panel_state() -> dict | None:
    """Return what's currently meant to be on the e-ink panel, or None.

    Returns `{"recipe_id": int, "page": int}` when a saved recipe is
    flagged as the active panel content, else None (panel is meant to
    be idle, or only an unsaved push was active and didn't get
    persisted). The caller (display_persistence.restore_on_startup)
    re-derives the rest from `recipe_id` via get_recipe.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT recipe_id, page FROM display_panel WHERE id = 1"
        ).fetchone()
    return {"recipe_id": row["recipe_id"], "page": row["page"]} if row else None


def set_panel_state(recipe_id: int, page: int = 1) -> None:
    """Persist the active panel recipe + page. Singleton row, UPSERT semantics."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO display_panel (id, recipe_id, page) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET recipe_id = excluded.recipe_id, "
            "page = excluded.page",
            (recipe_id, page),
        )


def clear_panel_state() -> None:
    """Drop the persisted panel state. Called when the display is cleared."""
    with _connect() as conn:
        conn.execute("DELETE FROM display_panel WHERE id = 1")
