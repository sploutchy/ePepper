# Roadmap

Items deferred from the code review — captured so they don't get lost.

## Decided against

### OTA firmware signing / TLS cert-pinning / boot-rollback

Evaluated and dropped. These (firmware signature verification with an offline
key, baking a CA to pin TLS on `/firmware/*`, and `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`
with a live-round-trip validity gate) defend against an on-path attacker
swapping the OTA blob, and against a clean-but-broken image bricking the panel.
For a single device on a home Wi-Fi this is fleet-management hardening: the
attacker already has to be inside the LAN, the download is Bearer-authed, and
the dual-partition `Update.end(true)` already prevents booting a *corrupt*
image. Not worth the offline-key infra, CI changes, and mandatory one-time
reflash for one household recipe screen. Recovery for a bad build is the
browser-USB reflasher at `/app/flash`.

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
