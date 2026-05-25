# ePepper — Incorrect-Comment Audit

Read-only audit for comments that **factually contradict** the code they
describe. Every file under `server/`, `esp32/`, and the JS in
`server/web/static/` was read in full and each suspected mismatch was
verified against the surrounding code before being listed here.

## Summary

| Severity | Count |
|----------|-------|
| High     | 3     |
| Medium   | 1     |
| Low      | 2     |
| **Total**| **6** |

Categories that yielded **nothing** (stated explicitly so the absence is
intentional, not an oversight):

- **Inline literal-value drift** (limits, timeouts, sizes): none. Checked
  `_MAX_HTML_BYTES`, `_PHOTO_MAX_BYTES` ("default 8 MB" vs `PHOTO_MAX_MB=8`),
  `_PENDING_MAX=32` ("or 32 newer pushes"), `STALE_HEARTBEAT_S=25*3600`
  ("≥25 h"), the battery curve breakpoints, the `30000`-ms OTA stall
  timeout, `MIN_SLEEP_S=60` / `MAX_SLEEP_S=172800` ("1 min" / "48 h"), and
  the `0.6` content-root threshold — all consistent with their comments.
- **ESP32 pin assignments**: none. `main.cpp` header (KEY0/GPIO3 Refresh,
  KEY1/GPIO4 Next, KEY2/GPIO5 Prev) matches `config.h.example`
  (`BTN_REFRESH 3`, `BTN_NEXT 4`, `BTN_PREV 5`). I2C pins (SDA 19 / SCL 20),
  SHT4x addr `0x44` / cmd `0xFD`, ADC divider note — all match.
- **Docstring param/return signatures**: no contradictions found across the
  Python modules (return-shape docstrings in `recipes.py`, `db.py`,
  `layout.py`, `state.py`, etc. all match the actual signatures/returns).
- **Copy-pasted comment describing a different function**: none found.

---

## High severity (actively misleading)

### H1. `server/cache/__init__.py` lines 3-8 — `DiskCache` interface described as `get`/`set`/**`delete`** plus a **TTL envelope** that don't exist

Quoted comment (module docstring):

> A small, dependency-free backend with a minimal interface (`get` / `set`
> / `delete`, plus optional TTL via an ``{"value": ..., "expires_at": ...}``
> envelope):

Actual code — `server/cache/disk.py` exposes only `get` and `set`:

```python
def get(self, key: str) -> Any | None:
    ...
def set(self, key: str, value: Any) -> None:
    ...
```

`grep "def "` over `disk.py` returns only `__init__`, `_load`, `_dump`,
`get`, `set`. There is **no `delete` method** and **no `expires_at` / TTL
handling anywhere** in the class. A maintainer reading the package
docstring would expect to call `cache.delete(...)` or rely on TTL expiry;
both would fail. Misleading on two independent counts.

### H2. `server/processing/jsonld.py` lines 3-7 — claims "The Telegram bot accepts a .json file" for JSON-LD ingest; no such handler exists

Quoted comment (module docstring):

> LLM-generated JSON-LD is the supported ingest path when a website isn't
> covered by recipe-scrapers or when the source is a photo (OCR via an LLM).
> **The Telegram bot accepts a .json file; this module locates the first
> Recipe object inside it** and maps it onto the same shape that
> `process_recipe_url` produces.

Actual code: `bot/handlers.py :: create_bot()` registers handlers only for
`filters.PHOTO` (`on_photo`), `filters.TEXT & ~filters.COMMAND` (`on_text`),
the named commands, the inline-button callbacks, and a catch-all
`filters.COMMAND`. There is **no `Document` / `.json` upload handler**:

```python
app.add_handler(MessageHandler(filters.PHOTO, on_photo))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
...
app.add_handler(MessageHandler(filters.COMMAND, on_unknown_command))
```

The web upload path (`api/web.py :: add_file`) likewise rejects everything
that isn't an image. In reality `parse_recipe_jsonld` is invoked **only**
from `processing/html_extract.py` for *embedded* `<script
type="application/ld+json">` blobs, and `resolve_url`/`synthetic_url` only
from the OCR path in `recipes.py`. The advertised ".json file upload"
ingest path does not exist, so the docstring describes a feature the code
does not implement.

### H3. `server/rendering/layout.py` lines 52-54 — references firmware constants `BTN_GLYPH_*_X` that do not exist in the ESP32 source

Quoted comment:

> X centers mirror the firmware constants in `esp32/include/config.h`
> (`BTN_GLYPH_*_X`) so they sit directly above the physical reTerminal keys.

```python
_BTN_GLYPH_Y = 2
_BTN_PREV_X = 450
_BTN_NEXT_X = 490
_BTN_REFRESH_X = 545
```

`grep -rn "BTN_GLYPH\|GLYPH" esp32/` returns **nothing**. `config.h.example`
defines only `BTN_REFRESH 3`, `BTN_NEXT 4`, `BTN_PREV 5` (GPIO numbers, not
glyph X positions), and explicitly states "All on-screen overlays … are
rendered by the server — no firmware-drawn UI any more." A maintainer told
to keep these X centers "in sync with the firmware constants" would go
looking for `BTN_GLYPH_PREV_X` etc. and find no such thing; the values are
purely server-side and have no firmware counterpart to mirror.

---

## Medium severity (stale but less likely to badly mislead)

### M1. `server/scheduler.py` — `midnight_loop` docstring lists chores as "(anniversary push, then DB backup)" but the body runs a Fooby prefetch in between

Quoted comments:

- Module docstring (lines 3-14) enumerates the midnight tick as exactly two
  steps: "1. Push a recipe … 2. Trigger a daily DB backup."
- `midnight_loop` docstring (lines 292-296): "run each day's chores **in
  order (anniversary push, then DB backup)**".

Actual body of `midnight_loop` (lines 306-328) runs **three** steps in
order: (1) anniversary/Fooby push, (2) `_prefetch_fooby_for(tomorrow)` to
populate tomorrow's preview, then (3) `backup.flush_if_dirty()`. The Fooby
*prefetch* chore between the push and the backup is omitted from both
docstrings, so the documented "anniversary push, then DB backup" sequence
is incomplete. Not dangerous (each step is independently guarded), but the
ordering description is factually short by one chore.

---

## Low severity (minor / cosmetic factual drift)

### L1. `server/display/state.py` line 8 — references a file `display_image.py` that doesn't exist (actual file is `display/image.py`)

Quoted comment (module docstring):

> BMP serialization lives in `display_image.py`; device telemetry
> (battery / heartbeat / alert hysteresis) in `device_telemetry.py`.

The BMP serializer is at `server/display/image.py` (module `display.image`).
There is no file named `display_image.py` and no `display_image` import
alias anywhere. The sibling reference in the same sentence,
`device_telemetry.py`, *is* a real top-level file, which makes the
`display_image.py` form read as a real path and slightly misleading. Low
because the codebase elsewhere uses `display_state` as the alias for
`display.state`, so a reader can infer `display/image.py` quickly.

### L2. `server/api/server.py` line 98 — grammatically/temporally muddled comment ("never read this") about the removed `page` field

Quoted comment in the `/version` handler:

> ```python
> # No `page` field (DES-D): the device computes its own page locally
> # and never read this. It was driven only by web-preview clicks.
> ```

The factual claim (the response intentionally omits a `page` field) is
correct — the returned dict has no `page` key. The wording "never read
this" refers to a field that no longer exists, so the sentence describes a
removed thing in a way that can momentarily confuse (it reads as if `this`
points at a present field). Purely cosmetic; the code matches the intent.
Listed only for completeness.
