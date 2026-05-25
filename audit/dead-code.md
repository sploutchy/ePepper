# ePepper dead-code audit

Read-only audit of `/home/user/ePepper`. No source was modified. Every
candidate below was grepped across the entire repo (Python, `*.html`
templates, `*.js`, `*.cpp`, `*.md`) before being classified. Reference
searches are quoted per finding.

## Summary counts

| Severity | Confirmed | Likely | Needs human review | Total |
|----------|-----------|--------|--------------------|-------|
| High     | 3         | 0      | 0                  | 3     |
| Medium   | 3         | 0      | 1                  | 4     |
| Low      | 3         | 0      | 2                  | 5     |
| **Total**| **9**     | **0**  | **3**              | **12**|

Tooling: `pyflakes` ran clean except the two unused imports below;
`vulture` was not installed. The codebase is small and tidy — most
modules have zero dead code. Notable: there is a cluster of dead/stale
*public* symbols in `library/db.py` whose docstrings describe features
(a `/surprise` bot command, a shift-click "hard delete", a `set_page`
display mutation) that do not exist anywhere in the tree.

---

## High severity
Genuinely misleading: a maintainer reading these would believe a
feature exists or a config knob works when it does not.

### H1. `library.random_recipe` — dead function + dead public export
- File: `server/library/db.py:797-818` (definition); re-exported at
  `server/library/__init__.py:18` and `:48`.
- Confidence: **Confirmed dead.**
- Reasoning: `grep -rn "random_recipe"` across `*.py/*.html/*.js`
  returns only the definition and the two `__init__` re-export lines —
  no caller. Its docstring says *"Used by the bot's /surprise
  command"* and the `exclude_id` param is *"used by the 'Another'
  button"*, but `grep -rni "surprise|exclude_id|another|re-roll"`
  finds no `/surprise` handler in `bot/handlers.py` (the registered
  commands are start/help/clear/status/comment/search only — see
  `_BOT_COMMANDS` and `create_bot`) and no "Another" button in any
  template. The entire feature was removed; the function and its
  `exclude_id` plumbing are orphaned.

### H2. `library.hard_delete_recipe` — dead function + dead public export
- File: `server/library/db.py:537-551` (definition); re-exported at
  `server/library/__init__.py:20` and `:50`.
- Confidence: **Confirmed dead.**
- Reasoning: `grep -rn "hard_delete_recipe"` returns only the
  definition + two `__init__` lines. The docstring claims it is a
  *"Hidden affordance accessed via shift-click on the web delete
  button."* But the delete button (`recipe.html:114-118`) is a plain
  `hx-delete="/app/recipes/{{ r.id }}"`, which routes to
  `delete_recipe` (`api/web.py:834`) → `library.delete_recipe` (the
  soft delete). `grep -rni "shiftKey|shift|hard"` over
  `server/web/templates` + `input-action.js` finds nothing (the only
  htmx.min.js matches are vendored-library noise). No shift-click
  handler exists, so `hard_delete_recipe` is unreachable.

### H3. `config.API_HOST` — dead config knob (documented but never applied)
- File: `server/config.py:23` (`API_HOST = os.environ.get("API_HOST",
  "0.0.0.0")`). Documented in `README.md:262` as the FastAPI bind
  address.
- Confidence: **Confirmed dead.**
- Reasoning: `grep -rn "API_HOST"` shows only the config definition and
  one occurrence in `main.py:61` — and that occurrence is just a string
  key inside `_print_config`'s `keys` tuple (it prints the value, it
  doesn't bind with it). The actual uvicorn bind hardcodes
  `host="0.0.0.0"` (`main.py:178`). So setting `API_HOST=127.0.0.1`
  (as README invites) has **no effect**. Either the binding should
  consume `config.API_HOST` or the knob + README row should be dropped.

---

## Medium severity
Stale but low blast-radius — redundant imports, a config var only read
by its own debug dumper, etc.

### M1. `main.py:16` — unused `import library`
- File: `server/main.py:16` (`import library`).
- Confidence: **Confirmed dead.**
- Reasoning: pyflakes flags it; `grep -n "\blibrary\b" main.py` shows
  the bare module is never used as `library.<attr>`. The only real use
  is `from library import init_db` on line 19 (and `init_db()` at
  :137). The line-16 import is redundant.

### M2. `scheduler.py:27` — unused `from display import state as display_state`
- File: `server/scheduler.py:27`.
- Confidence: **Confirmed dead.**
- Reasoning: pyflakes flags it; `grep -n "display_state" scheduler.py`
  returns only the import line. The scheduler reaches the display only
  via `from display.push import push_recipe_to_display` (:31). The
  `display_state` alias is never referenced.

### M3. `config.API_PORT` — parsed but only consumed by the config dumper
- File: `server/config.py:24` (`API_PORT = int(os.environ.get(...))`).
- Confidence: **Confirmed dead (the parsed variable, not the env var).**
- Reasoning: The env var IS honored — but directly, via
  `os.environ.get("API_PORT", "8080")` at `main.py:179`. The parsed
  `config.API_PORT` symbol itself is referenced only at `main.py:62`
  as a string key in `_print_config`. So the module-level
  `config.API_PORT` int is effectively unused for its stated purpose
  (binding); the server would behave identically if it were deleted.
  Lower than H3 because the feature (port override) does work via the
  env read — only the parsed constant is dead.

### M4. `processing/__init__.py` re-exports never imported — needs human review
- File: `server/processing/__init__.py:1-3`
  (`from .recipes import process_recipe_image, process_recipe_url` +
  `__all__`).
- Confidence: **Needs human review.**
- Reasoning: `grep -rn "from processing import"` shows callers only
  ever import submodules (`from processing import fooby_cache`,
  `from processing import llm`) or go straight to
  `from processing.recipes import ...` (e.g. `scheduler.py:33`,
  `api/web.py:35`, `bot/handlers.py:31`). Nobody does
  `from processing import process_recipe_image/process_recipe_url`, so
  the package-level re-export + `__all__` are an unused public surface.
  Flagged as review-only rather than confirmed because `__init__`
  re-exports are a conventional API affordance and removing them is a
  deliberate API decision, not a pure dead-code cleanup.

---

## Low severity
Cosmetic clutter; no behavioral impact.

### L1. `display/state.py:159` — `set_page()` defined but never called
- File: `server/display/state.py:159-166`.
- Confidence: **Confirmed dead.**
- Reasoning: `grep -rn "set_page"` across `*.py/*.html/*.js` returns
  the definition plus only **doc/comment** mentions
  (`display/state.py:24` and `display/persistence.py:40`, both of the
  form "after every mutation (set_recipe / set_page / clear)"). No code
  invokes it. There is no dynamic dispatch path to it — the only
  `getattr` in the tree is `getattr(config, key, None)` in
  `main.py:76`. Device page navigation moved entirely onto the ESP32
  (it owns `currentPage`; see `esp32/src/main.cpp:91` and the DES-D
  comments in `api/server.py:98`), which is exactly why this server
  mutation became orphaned. Low because it is just one small function,
  but worth noting the surrounding docstrings still advertise it as a
  live mutation path, which is mildly misleading.

### L2. `bot/handlers.py:325` — redundant f-string (no placeholders)
- File: `server/bot/handlers.py:325`
  (`library_lines = [f"<b>📚 Repertoire</b>", ...]`).
- Confidence: **Confirmed (lint, not dead code).**
- Reasoning: pyflakes: "f-string is missing placeholders". The `f`
  prefix on the first list element is a no-op. Harmless; included for
  completeness.

### L3. `config.RECIPE_HEIGHT` — pass-through constant equal to DISPLAY_HEIGHT
- File: `server/config.py:20`
  (`RECIPE_HEIGHT: int = DISPLAY_HEIGHT  # full panel`).
- Confidence: **Confirmed (live but vestigial).**
- Reasoning: It IS used (`rendering/layout.py` references it 6×), so
  not dead — but it is unconditionally aliased to `DISPLAY_HEIGHT`,
  so the renderer's distinction between "recipe height" and "display
  height" no longer means anything. Listed as Low/awareness, not for
  removal, since deleting it touches the renderer.

### L4. `cache/__init__.py` `DiskCache` re-export — needs human review
- File: `server/cache/__init__.py:14` + `__all__`.
- Confidence: **Needs human review.**
- Reasoning: The docstring invites `from cache import DiskCache`, but
  the sole consumer (`processing/fooby_cache.py:28`) imports
  `from cache.disk import DiskCache` directly. `grep -rn "from cache"`
  confirms no `from cache import DiskCache` caller. The re-export is an
  unused-but-intentional public facade; review before removing.

### L5. `rendering/__init__.py` `render_recipe` re-export — needs human review
- File: `server/rendering/__init__.py:1-3`.
- Confidence: **Needs human review.**
- Reasoning: Consumers import `from rendering.layout import render_recipe`
  (`display/state.py:19`) and `render_idle` (`display/image.py:12`)
  directly. Nothing does `from rendering import render_recipe`
  (`grep -rn "rendering" --include=*.py | grep import` shows only the
  `.layout` imports). The `__init__` re-export is unused and, oddly,
  exports only `render_recipe` while the other public entry point
  `render_idle` is not re-exported — a sign the facade has drifted.
  Same caveat as M4/L4: package-API decision, not pure dead code.

---

## Categories explicitly checked and found CLEAN

- **Unreachable code (after return/raise/break/continue):** none found.
  Spot-checked the early-return-heavy modules (`status_helpers.py`
  `humanize_date`, `recipes._detect_language`, the bot callback
  handlers); all branches reachable.
- **`if False` / constant-dead branches:** none found.
- **Commented-out code blocks:** none. The repo uses prose comments
  heavily but contains no abandoned commented-out code.
- **Unused declared dependencies (`requirements.txt`):** all 9 are
  used — `python-telegram-bot` (bot), `fastapi`+`uvicorn` (api/main),
  `Pillow` (rendering/images), `pillow-heif` (api/web HEIC opener),
  `recipe-scrapers` (recipes), `aiohttp` (llm/recipes/fooby),
  `beautifulsoup4` (html_extract/fooby), `jinja2` (web templates),
  `python-multipart` (FastAPI `UploadFile`/`Form` in api/web).
- **ESP32 firmware (`esp32/src/main.cpp`):** every declared function
  (lines 63-82) has a caller. `loop()` is empty by design (Arduino
  framework callback — not dead). `clearCacheAbove(0)` "wipe the lot"
  capability is documented but only ever invoked with `totalPages`;
  that is an unused *argument value*, not dead code, so not flagged.
- **Other library/db.py exports:** `set_translated_keywords`,
  `recipes_needing_translation`, `get_panel_state`/`set_panel_state`/
  `clear_panel_state`, `list_sources`/`list_tags`, session helpers,
  etc. — all have live callers (scheduler backfill, persistence,
  web filters, auth). Only `random_recipe` (H1) and
  `hard_delete_recipe` (H2) are dead.
- **`processing/jsonld.py`** (`synthetic_url`, `resolve_url`,
  `parse_recipe_jsonld`): all live — `resolve_url` ← `recipes.py:303`,
  `parse_recipe_jsonld` ← `html_extract.py:122`, `synthetic_url` ←
  `resolve_url`. (The module docstring's mention of a Telegram ".json
  file" upload path is stale prose — no such handler exists — but the
  functions themselves are reached via the OCR/JSON-LD ingest paths.)
- **`display_state` public API** (`get`, `set_recipe`, `clear`,
  `get_pages`, `consume_pending_displayed_bump`,
  `register_change_listener`): all have live callers; only `set_page`
  (L1) is orphaned.
