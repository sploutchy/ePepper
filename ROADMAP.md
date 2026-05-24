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

## Design

### DES-7: Remove server-side `/page/*` cursor; device computes page locally

`/page/next`, `/page/prev`, `/page/first` in `server/api/server.py` are
stateful — the server tracks the "current page" while the device asks for
next/prev. A server restart during an unsaved push loses the cursor, and the
design also blocks multi-device support (see DES-1 in the review).

**Partly done (on-device page cache).** The firmware now computes
next/prev/first/last locally (`computeLocalPage` in `esp32/src/main.cpp`)
and serves pages from a LittleFS cache, so the device no longer calls
`/page/*`. The cache is keyed on the new `content_hash` from `/version`.
See the "On-device page cache" section in the README.

Remaining:
1. Remove the `/page/*` endpoints from `server/api/server.py` once no
   un-upgraded firmware remains in the field.
2. Migrate the web status-page nav (`api/web.py:_change_page`) off the
   server-side cursor too, or accept that its "current page" is a
   preview-only cursor independent of the device.

Note the endpoints are intentionally retained for now: they still back the
web status preview's page nav and any device still on pre-cache firmware.
