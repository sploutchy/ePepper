# Roadmap

Items deferred from the code review — captured so they don't get lost.

## Security

### SEC-1: Sign firmware + pin TLS cert in the OTA flow

The OTA update path (`esp32/src/main.cpp:323-441`, `server/api/server.py:258-287`)
flashes whatever bytes the server returns over a `HTTPClient::begin(url)` call
that constructs an insecure HTTPS client — no `setCACert`, no signature
verification. An on-path attacker (open Wi-Fi neighbour, hostile ISP) can swap
`/firmware/download` and the device boots the malicious image on the next
daily wake.

Plan:
1. Bake the server CA (or a pinned leaf certificate) into the firmware build.
2. Use `NetworkClientSecure secure; secure.setCACert(ROOT_CA); http.begin(secure, url);`
   for both `/firmware/version` and `/firmware/download` (and ideally every
   other request — see the bearer-key leak below).
3. Sign the firmware blob with an offline key. Publish `firmware.bin` alongside
   a detached `firmware.sig`. The firmware verifies the signature against a
   public key baked into the build using `Update.setSignaturePubKey()` /
   `Update.setVerifyBegin()` before `Update.end(true)`.

This is a one-time hardware-reflash-required change: the trust anchors live in
the firmware itself.

### SEC-2: OTA rollback confirmation (gate boot-validity on a live server round-trip)

`checkForOTAUpdate()` (`esp32/src/main.cpp:456-563`) relies on the dual-partition
layout for safety: `Update.end(true)` only sets the boot partition after a
complete write, so a *partial/corrupted* download is never booted. But that's
the only failure it catches. A firmware that flashes cleanly yet can't actually
work — a Wi-Fi-stack regression, a bad `setCACert` after SEC-1 lands, a server
URL that no longer resolves — boots fine, marks itself valid, and becomes a
device that wakes daily and does nothing. There's no automatic fallback to the
last-known-good image because nothing ever declares the new one bad.

Plan:
1. Enable rollback in the build (`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`) so a
   freshly-OTA'd app boots in the `ESP_OTA_IMG_PENDING_VERIFY` state.
2. After the next wake's first successful server round-trip (a 2xx `/version`),
   call `esp_ota_mark_app_valid_cancel_rollback()` to confirm the image.
3. If the first post-OTA boot can't reach/authenticate the server within a
   bounded number of attempts, call
   `esp_ota_mark_app_invalid_rollback_and_reboot()` to drop back to the
   previous image automatically.

Pairs naturally with SEC-1 (a botched cert-pinning change is exactly the kind
of "flashes fine, can't connect" regression this guards against). Touches only
the firmware boot path + `platformio.ini` build flags; no server change.

## Design

### DES-7 (done): Removed server-side `/page/*` cursor; device computes page locally

`/page/next`, `/page/prev`, `/page/first`, `/page/last` in
`server/api/server.py` used to be stateful — the server tracked the
"current page" while the device asked for next/prev. A server restart
during an unsaved push lost the cursor, and the design also blocked
multi-device support (see DES-1 in the review).

Resolved in two steps:
1. The firmware now computes next/prev/first/last locally
   (`computeLocalPage` in `esp32/src/main.cpp`) and serves pages from a
   LittleFS cache keyed on `content_hash` from `/version` — see the
   "On-device page cache" section in the README.
2. The device-facing `/page/*` endpoints are gone. The only remaining
   server-side page cursor is `api/web.py:_change_page`
   (`/app/display/page/*`), which drives the status-page live preview
   only — accepted as a preview cursor independent of the panel rather
   than migrated away.
