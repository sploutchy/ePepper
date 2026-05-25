# ePepper — Documentation Drift Audit

Read-only audit comparing documentation (`README.md`, `DESIGN.md`,
`ROADMAP.md`, `server/.env.example`, `esp32/include/config.h.example`,
`esp32/platformio.ini`) against actual code behaviour, as of 2026-05-25.

## Summary

| Severity | Count |
|---|---|
| High | 2 |
| Medium | 6 |
| Low | 5 |
| **Total** | **13** |

High = misleading or broken (would cause a user/dev to fail or trust a
wrong claim). Medium = stale but won't break anything. Low = cosmetic /
minor.

---

## High

### H1 — README pin table documents a Buzzer (GPIO 45) that does not exist in firmware

- **Doc:** `README.md:677` — Pin mapping table:
  `| Buzzer | 45 | Active-high, MLT-8530 piezo |`. Also listed under the
  "**Peripherals**" group at `README.md:674`.
- **Code:** `esp32/include/config.h.example` defines no buzzer pin (only
  EPD, LED, buttons, I2C, battery — lines 47–72). `esp32/src/main.cpp`
  contains no buzzer / GPIO-45 / MLT-8530 reference anywhere (the only
  `45` in the file is the SHT4x temperature formula constant at
  `main.cpp:797`). A reader wiring or debugging against this table will
  look for buzzer code/config that isn't there.
- **Why High:** the pin table is presented as authoritative ("From the
  official schematic"), but a documented peripheral is entirely absent
  from both the config template and the firmware — a hardware/firmware
  developer following it is misled.

### H2 — README "Schema" block is materially out of date vs. the live schema

- **Doc:** `README.md:308-312` lists the schema as exactly four objects:
  - `recipes(id, url, title, parsed_json, lang, saved_at, last_displayed_at, displayed_count, created_at, deleted_at, source)`
  - `comments(...)`
  - `sessions(token_hash, created_at, expires_at)`
  - `recipes_fts` — "FTS5 virtual table over (title, ingredients, notes)."
- **Code:** `server/library/db.py` `_SCHEMA` (lines 40–125) shows:
  - `recipes` is missing the documented `translated_keywords` column
    (`db.py:53`) — a real, load-bearing column (LLM FR/DE search blob).
  - `recipes_fts` has **four** columns `(title, ingredients, notes,
    translated)` (`db.py:87-90`), not the three the README states.
  - Two whole tables are undocumented: `display_panel` (`db.py:99-104`,
    drives restart-survival of the panel) and `meta` (`db.py:121-124`,
    the `fts_rebuilt` sentinel). `schema_version` (`db.py:111`) is
    described prose-only in the migrations subsection but absent from the
    schema list.
- **Why High:** anyone reasoning about the DB (e.g. writing a migration,
  doing a manual restore, or inspecting `recipes.db`) is given a schema
  that omits a column and two tables and miscounts the FTS index columns.

---

## Medium

### M1 — README documents `?key=` query-param auth as "removed"; correct, but the device-fetch cook-bump nuance is undocumented (minor) — see also: tag filter and alphabetical sorts are fully undocumented

- **Doc:** `README.md` "Browsing the repertoire" (`README.md:166-184`)
  and the Web-app pages list (`README.md:277-282`) describe sorting as
  "Recently cooked / Most cooked / Least recently cooked" and filtering
  by "source" only. `DESIGN.md` "Repertoire" (`DESIGN.md:114-142`) only
  mentions "Sorting / filter dropdowns".
- **Code:** `server/api/web.py` `_VALID_SORTS` (`web.py:215-219`) also
  supports `source_az` and `source_za` (alphabetical-by-source sorts),
  and `index()` / `_search()` accept a **`tag`** query param
  (`web.py:450,468`) backed by `_sanitize_tag` (`web.py:241`) and the
  whole hashtag-filter machinery in `server/library/db.py`
  (`list_tags`, `_recipe_ids_with_tag`, `db.py:673-715`). Neither the
  `#tag` comment-filter feature nor the two source-alphabetical sorts
  appear anywhere in `README.md` or `DESIGN.md`.
- **Why Medium:** a working, user-facing feature set (tag filtering +
  two sort modes) is completely undocumented. Not broken, but a user
  won't discover it from the docs.

### M2 — README says the firmware sends `User-Agent: ePepper-device/<version>`; it is hard-coded to `1.0`

- **Doc:** `server/api/server.py:153-154` (the code docstring that the
  README's device-fetch behaviour leans on) and the general framing in
  README state the firmware identifies via
  `User-Agent: ePepper-device/<version>` — implying the running firmware
  version is carried in the UA.
- **Code:** every request in `esp32/src/main.cpp` sets a literal
  `http.setUserAgent("ePepper-device/1.0")` (`main.cpp:352, 373, 486,
  544, 721`). The `/1.0` is a fixed string, not the build's
  `FIRMWARE_VERSION`. The server only prefix-matches `ePepper-device/`
  (`server.py:145`,`148-155`), so behaviour is fine — but the
  "`<version>`" claim is inaccurate.
- **Why Medium:** misleading for anyone debugging device traffic who
  expects the firmware build number in the UA.

### M3 — `random_recipe` / `/surprise` is documented in code as a live bot feature but no such command/button exists

- **Doc (code-as-doc):** `server/library/db.py:797-818` docstring:
  "Used by the bot's `/surprise` command" and "used by the 'Another'
  button so a re-roll doesn't get blocked".
- **Code:** `server/bot/handlers.py` registers no `/surprise` handler
  (`create_bot`, `handlers.py:202-215`), `_BOT_COMMANDS`
  (`handlers.py:165-171`) has no surprise entry, and there is no
  "Another" button anywhere. `random_recipe` has no callers in the repo.
- **Why Medium:** a dangling reference to a removed/never-shipped
  feature. User-facing docs (README bot command table,
  `README.md:477-484`) correctly omit `/surprise`, so this is a
  code-comment drift rather than a user-facing one — hence Medium not
  High.

### M4 — `hard_delete_recipe` ("shift-click on the web delete button") has no shift-click wiring documented or present in the route

- **Doc (code-as-doc):** `server/library/db.py:537-545` docstring:
  "Hidden affordance accessed via shift-click on the web delete button."
- **Code:** the only delete route, `delete_recipe` in
  `server/api/web.py:834-847`, calls `library.delete_recipe` (the *soft*
  delete) unconditionally — there is no branch that invokes
  `hard_delete_recipe`, and nothing reads a shift-click signal. README
  "Editing recipes" (`README.md:212-215`) only documents the soft delete.
  `hard_delete_recipe` has no callers in the repo.
- **Why Medium:** the docstring promises an operator affordance that
  isn't wired up. Not user-facing in README, so Medium.

### M5 — README backup-restore claim about removing `-wal`/`-shm` sidecars vs. actual fixed filenames

- **Doc:** `README.md:425-427`: the restore CLI "removes any leftover
  `-wal` / `-shm` sidecars" — phrased generically.
- **Code:** `server/backup.py:229` removes exactly `recipes.db-wal` and
  `recipes.db-shm` (hard-coded names derived from `recipes.db`), not
  arbitrary sidecars. This is accurate in practice (the DB is always
  `recipes.db`), so it's a wording nuance rather than a bug — flagged for
  completeness.
- **Why Medium:** essentially correct; only a minor over-generalisation.

### M6 — README firmware.yml description undercounts the published artifacts

- **Doc:** `README.md:653-655`: ".github/workflows/firmware.yml rsyncs
  `firmware.bin` and a freshly-written `version.txt` to the VPS on every
  merge to main".
- **Code:** `.github/workflows/firmware.yml:179-184` rsyncs **four**
  files: `firmware.bin`, `version.txt`, `epepper-merged.bin`, and
  `manifest.json`. The README's own "Four files" bullet
  (`README.md:638-644`) lists all four correctly, so the "Publishing"
  sentence is internally inconsistent with the bullet just above it.
- **Why Medium:** the same doc both lists four files and says two are
  rsynced; reader has to reconcile.

---

## Low

### L1 — `server/api/server.py` `/display/clear` docstring claims a device chord that the firmware never issues

- **Doc (code-as-doc):** `server.py:160`: "Clear the panel. Fires on the
  device's PREV + REFRESH chord."
- **Code:** `esp32/src/main.cpp` never calls `/display/clear` (grep finds
  no reference). The only callers are the web status page
  (`web.py:753-759`) and the bot `/clear` (`handlers.py:295-300`). The
  README API table (`README.md:461`) correctly attributes it only to the
  web status page's Clear button. So this is an internal-docstring drift.
- **Why Low:** internal comment only; user-facing README is correct.

### L2 — ROADMAP cites `server/api/server.py:258-287` for the OTA endpoints; file is only 262 lines

- **Doc:** `ROADMAP.md:12`: "(`esp32/src/main.cpp:323-441`,
  `server/api/server.py:258-287`)".
- **Code:** `server/api/server.py` is 262 lines total; the OTA routes
  `/firmware/version` and `/firmware/download` live at lines 244–262. The
  cited `258-287` range overshoots the end of the file by 25 lines.
- **Why Low:** line-anchored citation has drifted; the prose still points
  at the right functions.

### L3 — ROADMAP SEC-1/SEC-2 main.cpp line ranges are stale

- **Doc:** `ROADMAP.md:11` cites the OTA flow at
  `esp32/src/main.cpp:323-441`, and `ROADMAP.md:30` cites
  `checkForOTAUpdate()` at `esp32/src/main.cpp:456-563`.
- **Code:** `checkForOTAUpdate()` actually spans `main.cpp:344-452`
  (declared `main.cpp:66`); lines `456-563` are `connectWiFi()` and
  following WiFi helpers, not the OTA function. The `323-441` range is
  roughly adjacent but off.
- **Why Low:** line numbers drifted after edits; the function name in the
  prose is still correct, so the reader can find it.

### L4 — README implies `LLM_TRANSLATE_MODEL` "Falls back to LLM_TEXT_MODEL if explicitly cleared"; matches code but the env example comment differs

- **Doc:** `README.md:261` and `server/.env.example:48-50` describe the
  translate model and its fallback.
- **Code:** `server/config.py:98-101` implements exactly the documented
  fallback (`... .strip() or LLM_TEXT_MODEL`). The `.env.example` comment
  (lines 48–49) still references "gemma3n was too weak" as rationale —
  harmless historical note, consistent with `config.py:92-97`. No
  functional drift; flagged only because the rationale comments are
  duplicated across two files and could diverge.
- **Why Low:** purely a maintenance/duplication note; behaviour matches.

### L5 — DESIGN.md references an external exploration path that isn't in the repo

- **Doc:** `DESIGN.md:9`: "see `claude/epepper-design-exploration-yVw1f`
  for the full set".
- **Code/tree:** no `claude/` directory exists in the repo (full `find`
  tree confirms). This is an external/historical reference, so it's
  expected to be absent, but a reader may try to open it.
- **Why Low:** clearly a pointer to an out-of-repo artifact; not a broken
  in-repo link.

---

## Categories checked that yielded NO drift

- **Environment variables (bidirectional):** every var read by
  `server/config.py` (`TELEGRAM_BOT_TOKEN`, `ALLOWED_USERS`, `API_HOST`,
  `API_PORT`, `API_KEY`, `WEB_URL`, `PHOTO_MAX_MB`, `DATA_DIR`, `TZ`,
  `DEVICE_WAKE_HOUR_LOCAL`, `BACKUP_CHAT_ID`, `LLM_API_URL`,
  `LLM_API_KEY`, `LLM_TEXT_MODEL`, `LLM_VISION_MODEL`,
  `LLM_TRANSLATE_MODEL`) is documented in both `README.md:246-263` and
  `server/.env.example`, and every var in `.env.example` is read by
  `config.py`. `main.py:_print_config` (`main.py:58-74`) lists the same
  set. No orphans in either direction.
- **HTTP endpoints (device-facing):** every route in the README API
  table (`README.md:456-465`) — `/version`, `/image`, `/image?page=N`,
  `/display/clear`, `POST/GET /device/status`, `/firmware/version`,
  `/firmware/download` — is registered in `server/api/server.py`, with
  matching auth semantics. No documented endpoint is missing, and no
  device-facing endpoint is undocumented. (`/page/*` is correctly
  documented as removed in `ROADMAP.md` DES-7 and absent from code.)
- **Web routes:** all README-documented `/app/` pages (`/app/`,
  `/app/add`, `/app/recipes/<id>`, `/app/status`, `/app/login`,
  `/app/flash`) exist in `server/api/web.py`. `flash.html` referenced by
  the flash route exists at `server/web/templates/flash.html`.
- **Bot commands:** the README command table (`README.md:477-484`)
  (`/comment`, `/search`, `/clear`, `/status`, `/start`, `/help`) exactly
  matches the handlers registered in `handlers.py:202-215` and
  `_BOT_COMMANDS` (`handlers.py:165-171`). No extra or missing
  user-facing commands. (See M3 for the `/surprise` code-comment drift.)
- **CLI flags:** `main.py --print-config` (`main.py:98-103`) matches
  `README.md:439-441`. `backup.py` subcommands `snapshot` / `restore` /
  `status` and the `-y/--yes` flag (`backup.py:264-283`) match
  `README.md:425-436`. The example `python backup.py restore
  recipes_<timestamp>.db.gz` (`README.md:430`) is valid against the
  parser.
- **Install/setup commands:** the `cp .env.example .env`, `mkdir -p
  ../data ../firmware`, `docker compose -f ../docker-compose.yml up`,
  `python3 -c "import secrets..."`, and PlatformIO `pio run -t upload` /
  `pio device monitor -b 115200` snippets (`README.md:109-145, 526-546`)
  all reference files/flags that exist and are correct.
- **Version / platform claims:** README "Python 3.12"
  (`README.md:129`) matches `server/Dockerfile:1` (`python:3.12-slim`).
  `BOARD_SCREEN_COMBO=520`, `default_8MB.csv`, and the Seeed_GFX /
  ArduinoJson deps (`README.md:577-611`) match
  `esp32/platformio.ini:19,30,32-34`. `API_PORT` default `8080` matches
  `config.py:24` and `docker-compose.yml:11`.
- **URL-scheme docs:** the three documented `url` shapes (http(s),
  `cookbook://name/slug`, `jsonld:<12-hex-digits>`) at
  `README.md:341-355` match `server/processing/jsonld.py` (sha1[:12],
  `jsonld.py:41`) and `server/status_helpers.py:source_name`
  (`status_helpers.py:105-139`). Tracking-param stripping
  (`utm_*`, `fbclid`, `ref`, `gclid`, `README.md:154`) matches
  `db.py:128-129`.
- **README relative links / anchors:** `[ROADMAP.md](ROADMAP.md)`
  (`README.md:697`) resolves; `#firmware` and `#on-device-page-cache`
  anchors (`README.md:142,229,555`) resolve to existing headings.
