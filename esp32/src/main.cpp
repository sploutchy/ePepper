/**
 * ePepper firmware — XIAO ESP32-S3 + reTerminal E1001
 *
 * Buttons (active-low, hw 10K pull-up + 100nF debounce):
 *   KEY0 (GPIO3)  — Refresh: poll server, redraw if changed (long-press: force)
 *   KEY1 (GPIO4)  — Next page
 *   KEY2 (GPIO5)  — Previous page
 *
 * Wake sources:
 *   - Timer: DAILY_REFRESH_INTERVAL_S — pull whatever ambient content the
 *     server has scheduled (e.g. an anniversary recipe).
 *   - Any of the 3 buttons via ext1 (active-low)
 *
 * Per wake:
 *   1. Decide intent (refresh / next / prev / timer)
 *   2. Page nav → serve the target page straight from the on-flash cache,
 *      NO WiFi. Falls back to the network only on a cache miss (e.g. a
 *      cold boot after the battery was pulled, which wipes RTC state).
 *   3. Refresh / timer → WiFi up; read battery + SHT40, POST /device/status
 *      (server bakes the fresh battery glyph into the next render); GET
 *      /version. If content_hash changed (or refresh was forced), download
 *      EVERY page and write it to LittleFS, then show the active page.
 *   4. Display BMP (always full refresh — server owns every pixel)
 *   5. Deep sleep
 *
 * On-flash page cache: the server renders the whole recipe up front, so all
 * pages share one stable content_hash (from /version). On a content change
 * we pull every page into LittleFS as /p<N>.bmp; subsequent next/prev turns
 * just blit the matching file. This trades one larger download
 * per recipe for Wi-Fi-free, lower-latency page turns. The cost: a page
 * turn's cached frame carries a stale battery glyph until the next refresh,
 * and the server's notion of the current page (used by the web status
 * preview) no longer tracks the device's local navigation.
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Update.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <FS.h>
#include <LittleFS.h>
#include "TFT_eSPI.h"
#include "config.h"

// Baked in by CI via -DFIRMWARE_VERSION=<github.run_number>. Local builds
// land at 0 — server reports 0 when no firmware is published, so a dev
// build won't trigger a spurious OTA against the un-versioned baseline.
#ifndef FIRMWARE_VERSION
#define FIRMWARE_VERSION 0
#endif

EPaper epaper;

// Upper bound on cached pages we'll prune. A recipe is typically 2–4 pages;
// this just caps the cleanup loop that unlinks stale /p<N>.bmp files when a
// new (shorter) recipe replaces a longer one.
#define MAX_CACHED_PAGES 32

enum WakeAction { WAKE_TIMER, WAKE_REFRESH, WAKE_NEXT, WAKE_PREV };

void handleRefresh(bool force = false);
void handlePageChange(const char* direction);
void handleTimerWake();
void checkForOTAUpdate();
bool waitForLongPress(int btnPin, int thresholdMs);
void connectWiFi();
bool pollServer();
bool downloadImage(int page);
void reportDeviceStatus();
int computeLocalPage(const char* direction);
bool cacheAllPages();
bool loadCachedPage(int page);
bool writeCacheFile(int page, uint8_t* data, size_t len);
void clearCacheAbove(int n);
void displayImage(uint8_t* data, size_t len);
bool readSHT40(float& tempC, float& rh);
float readBatteryVoltage();
void goToSleep(uint64_t seconds);
WakeAction detectWakeAction();
uint64_t computeSleepSeconds();

// ---- RTC-persistent state (survives deep sleep, lost on power loss) ----
// cachedHash is the content_hash of the recipe currently sitting in
// LittleFS. A page turn trusts the cache when it's non-empty and the
// /p<N>.bmp file exists; a refresh refills the cache when the server's
// content_hash differs from it. Cleared on power loss → first wake after
// a battery pull falls back to the network until the next refresh.
RTC_DATA_ATTR char cachedHash[16] = "";
RTC_DATA_ATTR int currentPage = 1;
RTC_DATA_ATTR int totalPages = 1;

// ---- Transient state ----
uint8_t* imageBuffer = nullptr;
size_t imageSize = 0;

// Whether LittleFS mounted this boot. False disables the on-flash cache
// (every page turn then falls back to the network), so a filesystem fault
// degrades gracefully instead of bricking navigation.
bool fsReady = false;

// content_hash seen on the most recent /version poll, staged here until a
// full cache rebuild succeeds so a partial/failed download never marks
// stale pages as valid (we only copy it into cachedHash from cacheAllPages
// on success).
char pendingContentHash[16] = "";

// Seconds the server told us to sleep on this wake cycle (from
// /version → next_wake_in_s). -1 = not yet known; the sleep helper
// falls back to DAILY_REFRESH_INTERVAL_S in that case. Transient so
// a stale value from a previous wake (taken minutes/hours ago) can't
// land us at the wrong time after a network blip.
int32_t nextWakeInS = -1;

// Set when a wake that needed the server couldn't reach it at all (WiFi
// join failed, TCP connect failed). computeSleepSeconds then retries in
// RETRY_SLEEP_S instead of drifting a full day on a single blip — before
// this, one failed 06:00 wake meant stale content until tomorrow. A
// server that IS reachable but errors (401 bad key, 5xx) keeps the daily
// cadence: hourly retries against a persistent misconfig would just burn
// battery.
bool serverUnreachable = false;

#ifndef RETRY_SLEEP_S
#define RETRY_SLEEP_S 3600  // 1 h — retry cadence after an unreachable-server wake
#endif


void setup() {
    Serial.begin(115200);
    delay(100);

    Serial.println("\n[ePepper] Wake");

    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, HIGH);  // LED off (active-low)

    pinMode(BTN_REFRESH, INPUT);
    pinMode(BTN_NEXT, INPUT);
    pinMode(BTN_PREV, INPUT);

    imageBuffer = (uint8_t*)ps_malloc(DISPLAY_WIDTH * DISPLAY_HEIGHT / 8 + 1024);
    if (!imageBuffer) {
        Serial.println("[ePepper] ERROR: Failed to allocate PSRAM buffer");
        goToSleep(DAILY_REFRESH_INTERVAL_S);
        return;
    }

    epaper.begin();

    // Mount the page cache. format-on-fail self-heals a corrupt FS and
    // initialises the partition the first time new firmware lands on a
    // device whose data partition was previously SPIFFS / blank.
    fsReady = LittleFS.begin(true);
    if (!fsReady) {
        Serial.println("[FS] LittleFS mount failed — page cache disabled");
    }

    WakeAction action = detectWakeAction();

    // Refresh is the only button with a long-press gesture (force full
    // redraw); paging is short-press only. Sample the refresh GPIO to tell
    // a tap from a hold before dispatching — ext1 only says which pin fired.
    bool isLong = false;
    if (action == WAKE_REFRESH) {
        isLong = waitForLongPress(BTN_REFRESH, LONG_PRESS_MS);
    }
    // Drain the held button before sleeping: ext1 wakes on ANY_LOW, so a
    // button still held at sleep entry would immediately re-wake us into a
    // loop. Wait for release first.
    int heldPin = -1;
    if (action == WAKE_REFRESH) heldPin = BTN_REFRESH;
    else if (action == WAKE_NEXT) heldPin = BTN_NEXT;
    else if (action == WAKE_PREV) heldPin = BTN_PREV;
    if (heldPin >= 0) {
        while (digitalRead(heldPin) == LOW) delay(10);
    }

    switch (action) {
        case WAKE_REFRESH:
            handleRefresh(isLong);
            break;
        case WAKE_NEXT:
            handlePageChange("next");
            break;
        case WAKE_PREV:
            handlePageChange("prev");
            break;
        case WAKE_TIMER:
            handleTimerWake();
            break;
    }

    goToSleep(computeSleepSeconds());
}


void loop() {}


// ---- Wake detection ----

WakeAction detectWakeAction() {
    esp_sleep_wakeup_cause_t reason = esp_sleep_get_wakeup_cause();
    if (reason != ESP_SLEEP_WAKEUP_EXT1) {
        Serial.println("[Wake] Timer");
        return WAKE_TIMER;
    }

    uint64_t mask = esp_sleep_get_ext1_wakeup_status();
    if (mask & (1ULL << BTN_NEXT)) { Serial.println("[Wake] Button: Next"); return WAKE_NEXT; }
    if (mask & (1ULL << BTN_PREV)) { Serial.println("[Wake] Button: Prev"); return WAKE_PREV; }
    Serial.println("[Wake] Button: Refresh");
    return WAKE_REFRESH;
}


// ---- Action handlers ----
// reportDeviceStatus runs BEFORE the image fetch so the server has fresh
// sensor values (battery + SHT40) to bake into the rendered glyphs for
// the very response we're about to download.

void handleRefresh(bool force) {
    Serial.printf("[Action] Refresh — polling server%s\n", force ? " (forced)" : "");

    connectWiFi();
    if (WiFi.status() != WL_CONNECTED) {
        serverUnreachable = true;  // retry sooner than the daily fallback
        return;
    }

    reportDeviceStatus();

    bool changed = pollServer();
    if (changed || force) {
        // (Re)download every page into the cache so later turns stay offline.
        // changed → it's a new recipe, so reset to page 1. force on unchanged
        // content → keep the current page, just refresh the on-flash copies.
        bool cached = cacheAllPages();
        if (changed) currentPage = 1;
        int show = (currentPage >= 1 && currentPage <= totalPages) ? currentPage : 1;

        bool shown = false;
        if (cached && loadCachedPage(show)) {
            displayImage(imageBuffer, imageSize);
            shown = true;
        } else if (downloadImage(show)) {
            // Cache write failed (FS full / unmounted) — show the page anyway.
            displayImage(imageBuffer, imageSize);
            shown = true;
        }
        if (shown) {
            Serial.println(force ? "[Action] Forced full redraw"
                                 : "[Action] New content displayed");
        }
    } else {
        Serial.println("[Action] No changes");
    }
}


// Page navigation is the whole point of the cache: compute the target page
// locally and blit it from LittleFS, no WiFi. The server cursor (/page/*)
// is no longer consulted — the device owns its current page. We only touch
// the network on a cache miss (cold boot after a battery pull wipes RTC
// state and the files predate it, or a corrupt read).
void handlePageChange(const char* direction) {
    Serial.printf("[Action] Page %s\n", direction);

    // Warm single-page: we genuinely know this recipe has one page (we have a
    // trustworthy cached content_hash), so the page-turn is a no-op. Cold state
    // — totalPages reset to 1 AND no cached hash, e.g. after a battery pull
    // wipes RTC — is NOT trustworthy: the recipe may well be multi-page, so we
    // fall through to a network refresh below rather than dead-ending here.
    if (totalPages <= 1 && cachedHash[0] != '\0') {
        Serial.println("[Action] Single page, nothing to do");
        return;
    }

    // Cold state: bring up WiFi, re-poll /version and rebuild the cache (reusing
    // the same path as handleRefresh) so the real page count is restored, then
    // serve the requested page below. If the recipe really is single-page after
    // the rebuild, the warm guard above will fire on the next press.
    if (totalPages <= 1 && cachedHash[0] == '\0') {
        Serial.println("[Action] Cold state — refreshing to recover page count");
        connectWiFi();
        if (WiFi.status() != WL_CONNECTED) {
            serverUnreachable = true;
            return;
        }
        reportDeviceStatus();
        pollServer();          // updates totalPages + stages pendingContentHash
        cacheAllPages();       // promotes the hash on success
        currentPage = 1;
        if (totalPages <= 1) {
            Serial.println("[Action] Single page, nothing to do");
            return;
        }
    }

    int newPage = computeLocalPage(direction);
    if (newPage == currentPage) {
        return;
    }

    // Offline fast path: cache is valid (we have a content_hash) and the
    // page file loads. No WiFi, no telemetry POST — that resumes on the
    // next refresh / timer wake.
    if (cachedHash[0] != '\0' && loadCachedPage(newPage)) {
        currentPage = newPage;
        displayImage(imageBuffer, imageSize);
        Serial.printf("[Action] Page %d/%d displayed (cache)\n", currentPage, totalPages);
        return;
    }

    // Cache miss — fall back to the network for this one page.
    Serial.println("[Action] Cache miss — fetching over WiFi");
    connectWiFi();
    if (WiFi.status() != WL_CONNECTED) {
        return;
    }

    reportDeviceStatus();

    if (downloadImage(newPage)) {
        currentPage = newPage;
        writeCacheFile(newPage, imageBuffer, imageSize);  // opportunistic refill
        displayImage(imageBuffer, imageSize);
        Serial.printf("[Action] Page %d/%d displayed (network)\n", currentPage, totalPages);
    }
}


// Daily ambient pull — server's anniversary scheduler may have rotated
// content in at midnight. Same wire shape as refresh, just unforced.
// After the display is up to date, opportunistically check for a new
// firmware build — keeps the OTA off the button-press path so the
// user never waits ~30 s for a flash on a quick tap.
void handleTimerWake() {
    Serial.println("[Timer] Daily wake — checking for ambient content");
    handleRefresh(false);
    checkForOTAUpdate();  // Reboots into the new image on success; returns otherwise.
}


// ---- OTA ----
// Polls /firmware/version; if the integer is higher than our build's
// FIRMWARE_VERSION, streams /firmware/download into the inactive OTA
// partition via Update.write() and reboots into it. WiFi must already
// be up — we piggy-back on the connection from the preceding handleRefresh.
//
// We don't use the HTTPUpdate helper because arduino-esp32's version
// can't attach custom request headers, and /firmware/download is
// Bearer-authed. Update.write() over a raw HTTPClient stream gives us
// header control + the same auto-rollback safety from the dual-partition
// layout (a partial / corrupted write is never marked as the boot
// partition, so the bootloader falls back to the running image).
void checkForOTAUpdate() {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[OTA] No WiFi, skipping check");
        return;
    }

    HTTPClient http;
    http.begin(String(SERVER_URL) + "/firmware/version");
    http.setUserAgent("ePepper-device/1.0");
    http.addHeader("Authorization", String("Bearer ") + API_KEY);
    int code = http.GET();
    if (code != 200) {
        Serial.printf("[OTA] /firmware/version returned %d\n", code);
        http.end();
        return;
    }
    long serverVersion = http.getString().toInt();
    http.end();

    if (serverVersion <= FIRMWARE_VERSION) {
        Serial.printf("[OTA] Up to date (running %d, server %ld)\n",
                      FIRMWARE_VERSION, serverVersion);
        return;
    }

    Serial.printf("[OTA] Update available: %d -> %ld, starting download…\n",
                  FIRMWARE_VERSION, serverVersion);

    http.begin(String(SERVER_URL) + "/firmware/download");
    http.setUserAgent("ePepper-device/1.0");
    http.addHeader("Authorization", String("Bearer ") + API_KEY);
    // Default read timeout is 1 s — too tight for the long pauses that
    // can happen mid-stream on a marginal Wi-Fi link.
    http.setTimeout(15000);
    code = http.GET();
    if (code != 200) {
        Serial.printf("[OTA] /firmware/download returned %d\n", code);
        http.end();
        return;
    }

    int contentLength = http.getSize();
    if (contentLength <= 0) {
        Serial.println("[OTA] Missing or zero Content-Length");
        http.end();
        return;
    }
    Serial.printf("[OTA] Image size: %d bytes\n", contentLength);

    if (!Update.begin(contentLength)) {
        Serial.printf("[OTA] Update.begin failed: %s\n", Update.errorString());
        http.end();
        return;
    }

    digitalWrite(LED_PIN, LOW);  // active-low LED on while flashing

    WiFiClient* stream = http.getStreamPtr();
    uint8_t buf[1024];
    size_t bytesRead = 0;
    int lastPctLogged = -1;
    unsigned long lastByteAt = millis();
    while (bytesRead < (size_t)contentLength) {
        size_t available = stream->available();
        if (available) {
            size_t toRead = min(available, sizeof(buf));
            toRead = min(toRead, (size_t)contentLength - bytesRead);
            size_t got = stream->readBytes(buf, toRead);
            if (got == 0) continue;
            if (Update.write(buf, got) != got) {
                Serial.printf("[OTA] Update.write failed: %s\n", Update.errorString());
                Update.abort();
                digitalWrite(LED_PIN, HIGH);
                http.end();
                return;
            }
            bytesRead += got;
            lastByteAt = millis();
            int pct = (int)(bytesRead * 100 / contentLength);
            if (pct / 10 > lastPctLogged / 10) {
                Serial.printf("[OTA] %d%% (%u/%d bytes)\n", pct, (unsigned)bytesRead, contentLength);
                lastPctLogged = pct;
            }
        } else {
            // Bail if the link goes quiet for too long — better to retry on
            // the next daily wake than hang forever and waste battery.
            if (millis() - lastByteAt > 30000) {
                Serial.println("[OTA] Stream stalled, aborting");
                Update.abort();
                digitalWrite(LED_PIN, HIGH);
                http.end();
                return;
            }
            delay(5);
        }
    }

    digitalWrite(LED_PIN, HIGH);
    http.end();

    if (!Update.end(true)) {  // true = set boot partition
        Serial.printf("[OTA] Update.end failed: %s\n", Update.errorString());
        return;
    }

    Serial.println("[OTA] Update applied, rebooting into new firmware…");
    delay(100);  // let Serial flush
    ESP.restart();
}


// ---- WiFi ----

void connectWiFi() {
    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("[WiFi] Already connected, RSSI: %d\n", WiFi.RSSI());
        return;
    }
    Serial.printf("[WiFi] Connecting to %s...\n", WIFI_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_TIMEOUT_MS) {
        delay(100);
    }

    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("[WiFi] Connected, IP: %s, RSSI: %d\n",
                      WiFi.localIP().toString().c_str(), WiFi.RSSI());
    } else {
        Serial.println("[WiFi] Connection failed");
    }
}


// ---- Server communication ----

bool pollServer() {
    HTTPClient http;
    String url = String(SERVER_URL) + "/version";
    http.begin(url);
    http.setUserAgent("ePepper-device/1.0");
    http.addHeader("Authorization", String("Bearer ") + API_KEY);

    int code = http.GET();
    if (code != 200) {
        // Negative codes are connection-level failures (DNS, TCP, TLS) —
        // treat like a WiFi blip and retry in RETRY_SLEEP_S. Positive
        // non-200s (bad API key → 401, 5xx) mean the server is up but
        // unhappy; keep the daily cadence rather than hammering it.
        if (code < 0) serverUnreachable = true;
        Serial.printf("[API] /version returned %d\n", code);
        http.end();
        return false;
    }

    String body = http.getString();
    http.end();

    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, body);
    if (err) {
        Serial.printf("[API] JSON parse error: %s\n", err.c_str());
        return false;
    }

    // content_hash is stable across page navigation (md5 over all pages),
    // so it only flips on a genuinely new render — exactly the cache-
    // invalidation signal we want. Fall back to the per-page `hash` if the
    // server predates content_hash (e.g. mid-rollout before the server
    // deploy lands), which keeps change detection working, just coarser.
    const char* contentHash = doc["content_hash"];
    if (!contentHash) contentHash = doc["hash"];
    totalPages = doc["total_pages"] | 1;
    // Server tells us when to next wake. Missing on old servers → -1
    // → fallback to DAILY_REFRESH_INTERVAL_S in computeSleepSeconds.
    nextWakeInS = doc["next_wake_in_s"] | -1;

    // Stage the hash; cacheAllPages() promotes it to cachedHash only after a
    // full successful rebuild so a failed download can't validate a stale cache.
    if (contentHash) {
        strncpy(pendingContentHash, contentHash, sizeof(pendingContentHash) - 1);
        pendingContentHash[sizeof(pendingContentHash) - 1] = '\0';
    } else {
        pendingContentHash[0] = '\0';
    }

    if (pendingContentHash[0] != '\0' && strcmp(pendingContentHash, cachedHash) != 0) {
        Serial.printf("[API] New content: content_hash=%s pages=%d\n",
                      pendingContentHash, totalPages);
        return true;
    }

    Serial.println("[API] No changes");
    return false;
}


bool downloadImage(int page) {
    HTTPClient http;
    String url = String(SERVER_URL) + "/image?page=" + String(page);
    http.begin(url);
    http.setUserAgent("ePepper-device/1.0");
    http.addHeader("Authorization", String("Bearer ") + API_KEY);

    int code = http.GET();
    if (code != 200) {
        Serial.printf("[API] /image returned %d\n", code);
        http.end();
        return false;
    }

    imageSize = http.getSize();
    size_t maxSize = DISPLAY_WIDTH * DISPLAY_HEIGHT / 8 + 1024;
    if (imageSize > maxSize) {
        Serial.printf("[API] Image too large: %d > %d\n", imageSize, maxSize);
        http.end();
        return false;
    }
    Serial.printf("[API] Image size: %d bytes\n", imageSize);

    WiFiClient* stream = http.getStreamPtr();
    size_t bytesRead = 0;
    unsigned long lastByteAt = millis();
    while (http.connected() && bytesRead < imageSize) {
        size_t available = stream->available();
        if (available) {
            size_t toRead = min(available, imageSize - bytesRead);
            stream->readBytes(imageBuffer + bytesRead, toRead);
            bytesRead += toRead;
            lastByteAt = millis();
        } else if (millis() - lastByteAt > 30000) {
            // Bail if the link goes quiet for too long — better to retry on
            // the next wake than hang forever and waste battery.
            Serial.println("[API] Stream stalled, aborting");
            http.end();
            return false;
        }
        delay(1);
    }

    http.end();
    Serial.printf("[API] Downloaded %d bytes\n", bytesRead);
    return bytesRead == imageSize;
}


// ---- Page cache (LittleFS) ----
// Pages live as /p<N>.bmp — exactly the bytes /image?page=N returns, so the
// display path is identical whether a frame came off the wire or off flash.

// Local cursor maths — next/prev wrap around the page count, now that the
// device owns its page. Returns the unchanged page on a single-page recipe
// or an unknown direction.
int computeLocalPage(const char* direction) {
    if (totalPages <= 1) return currentPage;
    if (strcmp(direction, "next") == 0)
        return currentPage < totalPages ? currentPage + 1 : 1;
    if (strcmp(direction, "prev") == 0)
        return currentPage > 1 ? currentPage - 1 : totalPages;
    return currentPage;
}


bool writeCacheFile(int page, uint8_t* data, size_t len) {
    if (!fsReady || data == nullptr || len == 0) return false;
    char path[16];
    snprintf(path, sizeof(path), "/p%d.bmp", page);
    File f = LittleFS.open(path, FILE_WRITE);
    if (!f) {
        Serial.printf("[FS] open(%s) for write failed\n", path);
        return false;
    }
    size_t written = f.write(data, len);
    f.close();
    if (written != len) {
        Serial.printf("[FS] short write %s: %u/%u\n", path, (unsigned)written, (unsigned)len);
        LittleFS.remove(path);  // don't leave a truncated page behind
        return false;
    }
    return true;
}


bool loadCachedPage(int page) {
    if (!fsReady) return false;
    char path[16];
    snprintf(path, sizeof(path), "/p%d.bmp", page);
    File f = LittleFS.open(path, FILE_READ);
    if (!f) return false;
    size_t len = f.size();
    size_t maxSize = DISPLAY_WIDTH * DISPLAY_HEIGHT / 8 + 1024;
    if (len == 0 || len > maxSize) {
        Serial.printf("[FS] %s bad size %u\n", path, (unsigned)len);
        f.close();
        return false;
    }
    size_t got = f.read(imageBuffer, len);
    f.close();
    if (got != len) {
        Serial.printf("[FS] short read %s: %u/%u\n", path, (unsigned)got, (unsigned)len);
        return false;
    }
    imageSize = len;
    return true;
}


// Unlink any cached page above `n` (e.g. when a 5-page recipe is replaced by
// a 2-page one). clearCacheAbove(0) wipes the lot.
void clearCacheAbove(int n) {
    if (!fsReady) return;
    for (int p = n + 1; p <= MAX_CACHED_PAGES; p++) {
        char path[16];
        snprintf(path, sizeof(path), "/p%d.bmp", p);
        if (LittleFS.exists(path)) LittleFS.remove(path);
    }
}


// Download every page of the current recipe into LittleFS. WiFi must already
// be up. On full success, promotes pendingContentHash → cachedHash (the cache
// is now valid for offline navigation) and prunes stale higher-numbered
// pages. Any failure leaves cachedHash untouched so we don't trust a partial
// cache; the caller falls back to a direct network draw.
bool cacheAllPages() {
    if (!fsReady) {
        Serial.println("[Cache] FS unavailable — skipping cache build");
        return false;
    }
    if (totalPages < 1 || totalPages > MAX_CACHED_PAGES) {
        Serial.printf("[Cache] Refusing to cache %d pages\n", totalPages);
        return false;
    }
    bool touchedFs = false;
    for (int p = 1; p <= totalPages; p++) {
        if (!downloadImage(p)) {
            Serial.printf("[Cache] Page %d download failed — aborting\n", p);
            // Only invalidate if we already overwrote lower pages with new
            // content (mixed cache). A download failure before ANY write —
            // e.g. a forced refresh while the server is unreachable —
            // leaves the existing cache fully intact and still trustworthy
            // for offline page turns.
            if (touchedFs) cachedHash[0] = '\0';
            return false;
        }
        if (!writeCacheFile(p, imageBuffer, imageSize)) {
            Serial.printf("[Cache] Page %d write failed — aborting\n", p);
            // writeCacheFile may have removed/truncated this page's file;
            // the cache can no longer be trusted as a whole.
            cachedHash[0] = '\0';
            return false;
        }
        touchedFs = true;
    }
    clearCacheAbove(totalPages);
    // Never promote an empty hash: a hashless server (no content_hash) would
    // otherwise poison cachedHash so the cachedHash[0] != '\0' guard fails
    // forever and every page turn falls back to the network. Leave cachedHash
    // as-is (invalid) so the cache is intentionally not trusted offline.
    if (pendingContentHash[0] == '\0') {
        Serial.printf("[Cache] Stored %d page(s), no content_hash — cache not promoted\n", totalPages);
        return true;
    }
    strncpy(cachedHash, pendingContentHash, sizeof(cachedHash) - 1);
    cachedHash[sizeof(cachedHash) - 1] = '\0';
    Serial.printf("[Cache] Stored %d page(s), content_hash=%s\n", totalPages, cachedHash);
    return true;
}


void reportDeviceStatus() {
    float battV = readBatteryVoltage();
    int batteryMv = (int)(battV * 1000);

    float tempC = 0, rh = 0;
    bool envOk = readSHT40(tempC, rh);

    String url = String(SERVER_URL) + "/device/status"
                 + "?battery_mv=" + String(batteryMv)
                 + "&rssi=" + String(WiFi.RSSI())
                 + "&firmware_version=" + String(FIRMWARE_VERSION);
    if (envOk) {
        url += "&temperature_c=" + String(tempC, 1);
        url += "&humidity_pct=" + String(rh, 0);
    }

    HTTPClient http;
    http.begin(url);
    http.setUserAgent("ePepper-device/1.0");
    http.addHeader("Authorization", String("Bearer ") + API_KEY);

    int code = http.POST("");

    if (envOk) {
        Serial.printf("[API] Status: %dmV  %.1fC  %.0f%%  rssi=%d → %d\n",
                      batteryMv, tempC, rh, WiFi.RSSI(), code);
    } else {
        Serial.printf("[API] Status: %dmV  rssi=%d → %d  (no SHT40)\n",
                      batteryMv, WiFi.RSSI(), code);
    }
    http.end();
}


// ---- Battery ----

float readBatteryVoltage() {
    pinMode(BATTERY_ENABLE_PIN, OUTPUT);
    digitalWrite(BATTERY_ENABLE_PIN, HIGH);
    delay(10);  // ADC settle

    // Average several samples — a single ESP32 ADC read is noisy enough
    // (tens of mV) to flap the server's low-battery threshold.
    const int kSamples = 8;
    uint32_t sum = 0;
    for (int i = 0; i < kSamples; i++) {
        sum += analogRead(BATTERY_ADC_PIN);
        delay(2);
    }
    int raw = sum / kSamples;
    digitalWrite(BATTERY_ENABLE_PIN, LOW);

    // ESP32-S3 ADC: 12-bit (0-4095), 0-3.3 V range, ~2:1 divider on this board.
    float voltage = (raw / 4095.0f) * 3.3f * 2.0f;
    Serial.printf("[Battery] ADC raw=%d → %.2fV\n", raw, voltage);
    return voltage;
}


// ---- SHT40 ambient sensor ----
// Dependency-free I²C reader. CRC poly x^8+x^5+x^4+1 (0x31), init 0xFF —
// per the SHT4x datasheet §4.

static uint8_t sht4xCRC(const uint8_t* data, size_t len) {
    uint8_t crc = 0xFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int b = 0; b < 8; b++) {
            crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
        }
    }
    return crc;
}


bool readSHT40(float& tempC, float& rh) {
    Wire.begin(I2C_SDA, I2C_SCL);   // idempotent — no-op on ESP32 Arduino
    Wire.setClock(100000);

    Wire.beginTransmission(SHT4X_ADDR);
    Wire.write(SHT4X_CMD_MEAS);
    if (Wire.endTransmission() != 0) {
        Serial.println("[SHT40] NAK on command — sensor not responding");
        return false;
    }
    delay(10);  // datasheet: 8.2 ms max for high-precision

    size_t got = Wire.requestFrom((uint8_t)SHT4X_ADDR, (uint8_t)6);
    if (got != 6) {
        Serial.printf("[SHT40] short read: %u/6 bytes\n", (unsigned)got);
        return false;
    }
    uint8_t b[6];
    for (int i = 0; i < 6; i++) b[i] = Wire.read();

    if (sht4xCRC(b, 2) != b[2] || sht4xCRC(b + 3, 2) != b[5]) {
        Serial.println("[SHT40] CRC mismatch");
        return false;
    }

    uint16_t tRaw  = ((uint16_t)b[0] << 8) | b[1];
    uint16_t rhRaw = ((uint16_t)b[3] << 8) | b[4];
    tempC = -45.0f + 175.0f * tRaw / 65535.0f;
    rh    = -6.0f  + 125.0f * rhRaw / 65535.0f;
    if (rh < 0.0f)   rh = 0.0f;
    if (rh > 100.0f) rh = 100.0f;
    return true;
}


// ---- Display ----
// The server owns every pixel — battery glyph, button glyphs, page
// indicator, recipe content. Firmware just blits the BMP.

void displayImage(uint8_t* data, size_t len) {
    if (len < 62) {
        Serial.printf("[Display] BMP too small: %d bytes\n", len);
        return;
    }

    uint32_t pixelOffset;
    int32_t bmpWidth, bmpHeight;
    memcpy(&pixelOffset, data + 10, sizeof(pixelOffset));
    memcpy(&bmpWidth, data + 18, sizeof(bmpWidth));
    memcpy(&bmpHeight, data + 22, sizeof(bmpHeight));

    bool topDown = bmpHeight < 0;
    int h = topDown ? -bmpHeight : bmpHeight;
    int w = bmpWidth;
    int rowBytes = ((w + 31) / 32) * 4;

    if (pixelOffset + (size_t)rowBytes * h > len) {
        Serial.printf("[Display] BMP invalid: offset=%u w=%d h=%d row=%d len=%d\n",
                      pixelOffset, w, h, rowBytes, len);
        return;
    }

    uint8_t* pixels = data + pixelOffset;

    // BMP is bottom-up by default — flip rows so drawBitmap renders right-side-up
    if (!topDown && rowBytes <= 128) {
        uint8_t tmp[128];
        for (int y = 0; y < h / 2; y++) {
            uint8_t* top = pixels + y * rowBytes;
            uint8_t* bot = pixels + (h - 1 - y) * rowBytes;
            memcpy(tmp, top, rowBytes);
            memcpy(top, bot, rowBytes);
            memcpy(bot, tmp, rowBytes);
        }
    }

    epaper.fillScreen(TFT_WHITE);
    // PIL "1" mode → BMP: bit 1 = white, bit 0 = black
    epaper.drawBitmap(0, 0, pixels, w, h, TFT_WHITE, TFT_BLACK);
    epaper.update();

    Serial.printf("[Display] Pushed %dx%d image to panel\n", w, h);
}


// Returns true if the button is still held LOW after thresholdMs, false
// if released earlier.
bool waitForLongPress(int btnPin, int thresholdMs) {
    unsigned long start = millis();
    while ((long)(millis() - start) < thresholdMs) {
        if (digitalRead(btnPin) != LOW) return false;
        delay(20);
    }
    return true;
}


// ---- Sleep ----

// Pick the sleep duration: server-provided next_wake_in_s when present
// and sane, otherwise the 24-h fallback — shortened to RETRY_SLEEP_S when
// this wake needed the server and couldn't reach it, so a single blip
// doesn't cost a full day of stale content. The bounds are a sanity check
// against a clock-skewed server (e.g. a value of 1 s would burn the
// battery; a value of 30 days would silently kill the device).
uint64_t computeSleepSeconds() {
    if (nextWakeInS < (int32_t)MIN_SLEEP_S || nextWakeInS > (int32_t)MAX_SLEEP_S) {
        return serverUnreachable ? (uint64_t)RETRY_SLEEP_S
                                 : (uint64_t)DAILY_REFRESH_INTERVAL_S;
    }
    return (uint64_t)nextWakeInS;
}


void goToSleep(uint64_t seconds) {
    Serial.printf("[ePepper] Sleeping for %llu seconds...\n\n", seconds);

    if (WiFi.status() == WL_CONNECTED) {
        WiFi.disconnect(true);
    }

    // Put the EPD controller into deep sleep before the SoC sleeps; the
    // panel would otherwise hold its drive current until the next boot.
    epaper.sleep();

    uint64_t buttonMask = (1ULL << BTN_REFRESH) | (1ULL << BTN_NEXT) | (1ULL << BTN_PREV);
    esp_sleep_enable_ext1_wakeup(buttonMask, ESP_EXT1_WAKEUP_ANY_LOW);
    esp_sleep_enable_timer_wakeup(seconds * 1000000ULL);

    if (imageBuffer) {
        free(imageBuffer);
        imageBuffer = nullptr;
    }

    esp_deep_sleep_start();
}
