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

Plan:
1. Remove the `/page/*` endpoints from `server/api/server.py`.
2. Have the firmware compute next/prev locally using its RTC-stored
   `currentPage` and `totalPages` (`esp32/src/main.cpp:557-592`).
3. Replace cursor calls with `GET /image?page=N` only (already supported).

Breaking change: old firmware in the field will lose page navigation until
re-flashed. Roll out the firmware OTA first, then drop the server endpoints.
