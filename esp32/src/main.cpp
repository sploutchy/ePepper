/**
 * ePepper firmware — XIAO ESP32-S3 + reTerminal E1001
 *
 * Buttons (active-low, hw 10K pull-up + 100nF debounce):
 *   KEY0 (GPIO3)  — Refresh: poll server, redraw if changed (long-press: force)
 *   KEY1 (GPIO4)  — Next page (long-press: last page)
 *   KEY2 (GPIO5)  — Previous page (long-press: first page)
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
 * we pull every page into LittleFS as /p<N>.bmp; subsequent next/prev/first/
 * last turns just blit the matching file. This trades one larger download
 * per recipe for Wi-Fi-free, lower-latency page turns. The cost: a page
 * turn's cached frame carries a stale battery glyph until the next refresh,
 * and the server's notion of the current page (used by the web status
 * preview) no longer tracks the device's local navigation.
 *
 * Time is set from the HTTP Date header in every server response, so we
 * never call out to NTP. Logs stay roughly correct as a side effect.
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Update.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <FS.h>
#include <LittleFS.h>
#include <sys/time.h>
#include <time.h>
#include "TFT_eSPI.h"
#include "EPaperFixed.h"  // Kept dormant: subclass adds partial-refresh override we no longer call.
#include "config.h"

// Baked in by CI via -DFIRMWARE_VERSION=<github.run_number>. Local builds
// land at 0 — server reports 0 when no firmware is published, so a dev
// build won't trigger a spurious OTA against the un-versioned baseline.
#ifndef FIRMWARE_VERSION
#define FIRMWARE_VERSION 0
#endif

EPaperFixed epaper;

// Upper bound on cached pages we'll prune. A recipe is typically 2–4 pages;
// this just caps the cleanup loop that unlinks stale /p<N>.bmp files when a
// new (shorter) recipe replaces a longer one.
#define MAX_CACHED_PAGES 32

enum WakeAction { WAKE_TIMER, WAKE_REFRESH, WAKE_NEXT, WAKE_PREV, WAKE_CLEAR };

void handleRefresh(bool force = false);
void handlePageChange(const char* direction);
void handleClear();
void handleTimerWake();
void checkForOTAUpdate();
void warmWindow();
bool waitForLongPress(int btnPin, int thresholdMs);
void connectWiFi();
void showErrorFrame();
bool pollServer();
bool downloadImage(int page);
void reportDeviceStatus();
int computeLocalPage(const char* direction);
bool cacheAllPages();
bool loadCachedPage(int page);
bool writeCacheFile(int page, uint8_t* data, size_t len);
void clearCacheAbove(int n);
void clearCache();
void displayImage(uint8_t* data, size_t len);
void showErrorFrame(const char* headline, const char* detail);
bool readSHT40(float& tempC, float& rh);
float readBatteryVoltage();
void buzzerBeep(int count, int duration_ms);
void goToSleep(uint64_t seconds);
WakeAction detectWakeAction();
void collectDateHeader(HTTPClient& http);
void applyDateHeader(HTTPClient& http);
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
RTC_DATA_ATTR int wakeCount = 0;

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


void setup() {
    Serial.begin(115200);
    delay(100);

    wakeCount++;
    Serial.printf("\n[ePepper] Wake #%d\n", wakeCount);

    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, HIGH);  // LED off (active-low)
    pinMode(BUZZER_PIN, OUTPUT);
    digitalWrite(BUZZER_PIN, LOW);

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

    // For button wakes, sample the GPIO to distinguish a tap from a long
    // press before dispatching. ext1 only tells us which pin fired, not
    // how long it's been held; the button is typically still down here.
    // The chord (WAKE_CLEAR) has no long-press semantics — we just wait
    // for both pins to release so the warm window doesn't re-fire.
    bool isLong = false;
    int btnPin = -1;
    if (action == WAKE_REFRESH) btnPin = BTN_REFRESH;
    else if (action == WAKE_NEXT) btnPin = BTN_NEXT;
    else if (action == WAKE_PREV) btnPin = BTN_PREV;
    if (btnPin >= 0) {
        isLong = waitForLongPress(btnPin, LONG_PRESS_MS);
        if (isLong) {
            // Drain the rest of the hold so the warm window doesn't
            // immediately re-fire on the same physical press.
            while (digitalRead(btnPin) == LOW) delay(10);
        }
    } else if (action == WAKE_CLEAR) {
        while (digitalRead(BTN_PREV) == LOW || digitalRead(BTN_REFRESH) == LOW) {
            delay(10);
        }
    }

    switch (action) {
        case WAKE_REFRESH:
            handleRefresh(isLong);
            break;
        case WAKE_NEXT:
            handlePageChange(isLong ? "last" : "next");
            break;
        case WAKE_PREV:
            handlePageChange(isLong ? "first" : "prev");
            break;
        case WAKE_CLEAR:
            handleClear();
            break;
        case WAKE_TIMER:
            handleTimerWake();
            break;
    }

    // After a button press, hold awake (WiFi connected) for a warm window
    // so quick follow-up presses skip the cold-start + WiFi reconnect.
    if (action != WAKE_TIMER) {
        warmWindow();
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

    // PREV+REFRESH chord = force-clear. Sample the GPIOs directly after a
    // brief settle so we catch the chord even when the user releases the
    // two buttons a few ms apart (ext1 only carries the bits that fired
    // simultaneously; a slightly-delayed second press wouldn't show up).
    delay(15);
    if (digitalRead(BTN_PREV) == LOW && digitalRead(BTN_REFRESH) == LOW) {
        Serial.println("[Wake] Chord: Prev+Refresh (clear)");
        return WAKE_CLEAR;
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
    digitalWrite(LED_PIN, LOW);
    buzzerBeep(1, 50);

    connectWiFi();
    if (WiFi.status() != WL_CONNECTED) {
        showErrorFrame("Wi-Fi failed", "Check SSID / signal");
        buzzerBeep(3, 100);
        showErrorFrame();
        digitalWrite(LED_PIN, HIGH);
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

    digitalWrite(LED_PIN, HIGH);
}


// Page navigation is the whole point of the cache: compute the target page
// locally and blit it from LittleFS, no WiFi. The server cursor (/page/*)
// is no longer consulted — the device owns its current page. We only touch
// the network on a cache miss (cold boot after a battery pull wipes RTC
// state and the files predate it, or a corrupt read).
void handlePageChange(const char* direction) {
    Serial.printf("[Action] Page %s\n", direction);
    digitalWrite(LED_PIN, LOW);

    if (totalPages <= 1) {
        Serial.println("[Action] Single page, nothing to do");
        buzzerBeep(2, 50);
        digitalWrite(LED_PIN, HIGH);
        return;
    }

    int newPage = computeLocalPage(direction);
    if (newPage == currentPage) {
        digitalWrite(LED_PIN, HIGH);
        return;
    }

    // Offline fast path: cache is valid (we have a content_hash) and the
    // page file loads. No WiFi, no telemetry POST — that resumes on the
    // next refresh / timer wake.
    if (cachedHash[0] != '\0' && loadCachedPage(newPage)) {
        currentPage = newPage;
        displayImage(imageBuffer, imageSize);
        buzzerBeep(1, 30);
        Serial.printf("[Action] Page %d/%d displayed (cache)\n", currentPage, totalPages);
        digitalWrite(LED_PIN, HIGH);
        return;
    }

    // Cache miss — fall back to the network for this one page.
    Serial.println("[Action] Cache miss — fetching over WiFi");
    connectWiFi();
    if (WiFi.status() != WL_CONNECTED) {
        showErrorFrame("Wi-Fi failed", "Check SSID / signal");
        buzzerBeep(3, 100);
        showErrorFrame();
        digitalWrite(LED_PIN, HIGH);
        return;
    }

    reportDeviceStatus();

    if (downloadImage(newPage)) {
        currentPage = newPage;
        writeCacheFile(newPage, imageBuffer, imageSize);  // opportunistic refill
        displayImage(imageBuffer, imageSize);
        buzzerBeep(1, 30);
        Serial.printf("[Action] Page %d/%d displayed (network)\n", currentPage, totalPages);
    }

    digitalWrite(LED_PIN, HIGH);
}


// Force-clear the panel — fires on the PREV+REFRESH chord. Tells the
// server to drop its display state, then fetches and shows the idle
// hint image so the user knows which button wakes content back up.
void handleClear() {
    Serial.println("[Action] Clear (chord)");
    digitalWrite(LED_PIN, LOW);
    buzzerBeep(2, 50);

    connectWiFi();
    if (WiFi.status() != WL_CONNECTED) {
        showErrorFrame("Wi-Fi failed", "Check SSID / signal");
        buzzerBeep(3, 100);
        showErrorFrame();
        digitalWrite(LED_PIN, HIGH);
        return;
    }

    reportDeviceStatus();

    HTTPClient http;
    String url = String(SERVER_URL) + "/display/clear";
    http.begin(url);
    http.addHeader("Authorization", String("Bearer ") + API_KEY);
    collectDateHeader(http);
    int code = http.POST("");
    applyDateHeader(http);
    http.end();
    if (code != 200) {
        Serial.printf("[API] /display/clear returned %d\n", code);
        char detail[24];
        snprintf(detail, sizeof(detail), "HTTP %d", code);
        showErrorFrame("Server error", detail);
        buzzerBeep(3, 100);
        digitalWrite(LED_PIN, HIGH);
        return;
    }

    currentPage = 1;
    totalPages = 1;
    clearCache();  // drop the on-flash pages + content_hash so a re-push refetches
    if (downloadImage(1)) {
        displayImage(imageBuffer, imageSize);
        Serial.println("[Action] Cleared display");
    }

    digitalWrite(LED_PIN, HIGH);
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
    http.addHeader("Authorization", String("Bearer ") + API_KEY);
    collectDateHeader(http);

    int code = http.GET();
    if (code != 200) {
        Serial.printf("[API] /version returned %d\n", code);
        http.end();
        return false;
    }

    applyDateHeader(http);

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
    http.addHeader("Authorization", String("Bearer ") + API_KEY);
    collectDateHeader(http);

    int code = http.GET();
    if (code != 200) {
        Serial.printf("[API] /image returned %d\n", code);
        http.end();
        return false;
    }
    applyDateHeader(http);

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

// Local cursor maths — mirrors the wrap/clamp the server's /page/* used to
// do, now that the device owns its page. Returns the unchanged page on a
// single-page recipe or an unknown direction.
int computeLocalPage(const char* direction) {
    if (totalPages <= 1) return currentPage;
    if (strcmp(direction, "next") == 0)
        return currentPage < totalPages ? currentPage + 1 : 1;
    if (strcmp(direction, "prev") == 0)
        return currentPage > 1 ? currentPage - 1 : totalPages;
    if (strcmp(direction, "first") == 0) return 1;
    if (strcmp(direction, "last") == 0)  return totalPages;
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


void clearCache() {
    clearCacheAbove(0);
    cachedHash[0] = '\0';
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
    for (int p = 1; p <= totalPages; p++) {
        if (!downloadImage(p) || !writeCacheFile(p, imageBuffer, imageSize)) {
            Serial.printf("[Cache] Page %d build failed — aborting\n", p);
            // We may have already overwritten lower pages with new content
            // while higher ones are still stale. Invalidate so page turns
            // fall back to the network instead of serving a mixed cache; the
            // next refresh that sees the same content_hash rebuilds cleanly.
            cachedHash[0] = '\0';
            return false;
        }
    }
    clearCacheAbove(totalPages);
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
    http.addHeader("Authorization", String("Bearer ") + API_KEY);
    collectDateHeader(http);

    int code = http.POST("");
    applyDateHeader(http);

    if (envOk) {
        Serial.printf("[API] Status: %dmV  %.1fC  %.0f%%  rssi=%d → %d\n",
                      batteryMv, tempC, rh, WiFi.RSSI(), code);
    } else {
        Serial.printf("[API] Status: %dmV  rssi=%d → %d  (no SHT40)\n",
                      batteryMv, WiFi.RSSI(), code);
    }
    http.end();
}


// ---- Time sync via HTTP Date header ----
// Replaces NTP: every server response carries a Date header per RFC 7231,
// so we get a roughly-correct clock as a side effect of any wake.

void collectDateHeader(HTTPClient& http) {
    static const char* keys[] = {"Date"};
    http.collectHeaders(keys, 1);
}


void applyDateHeader(HTTPClient& http) {
    String dateStr = http.header("Date");
    if (dateStr.length() < 25) return;

    // RFC 7231: "Sun, 17 May 2026 12:34:56 GMT" — fixed layout, ASCII month abbr.
    static const char MONTHS[] = "JanFebMarAprMayJunJulAugSepOctNovDec";
    int day = 0, year = 0, hour = 0, minute = 0, second = 0;
    char monStr[4] = {0};
    if (sscanf(dateStr.c_str(), "%*3s, %d %3s %d %d:%d:%d",
               &day, monStr, &year, &hour, &minute, &second) != 6) {
        Serial.printf("[Time] Bad Date header: %s\n", dateStr.c_str());
        return;
    }
    const char* m = strstr(MONTHS, monStr);
    if (!m) return;

    struct tm t = {};
    t.tm_mday = day;
    t.tm_mon = (m - MONTHS) / 3;
    t.tm_year = year - 1900;
    t.tm_hour = hour;
    t.tm_min = minute;
    t.tm_sec = second;
    t.tm_isdst = 0;

    // The Date header is GMT. mktime() treats the struct as local time, so
    // we briefly force TZ=UTC to get the right epoch independent of the
    // device's notional local zone.
    setenv("TZ", "UTC", 1); tzset();
    time_t epoch = mktime(&t);
    unsetenv("TZ"); tzset();
    if (epoch <= 0) return;

    struct timeval tv = { .tv_sec = epoch, .tv_usec = 0 };
    settimeofday(&tv, nullptr);
}


// ---- Battery ----

float readBatteryVoltage() {
    pinMode(BATTERY_ENABLE_PIN, OUTPUT);
    digitalWrite(BATTERY_ENABLE_PIN, HIGH);
    delay(10);  // ADC settle

    int raw = analogRead(BATTERY_ADC_PIN);
    digitalWrite(BATTERY_ENABLE_PIN, LOW);

    // ESP32-S3 ADC: 12-bit (0-4095), 0-3.3 V range, ~2:1 divider on this board.
    float voltage = (raw / 4095.0f) * 3.3f * 2.0f;
    Serial.printf("[Battery] ADC raw=%d → %.2fV\n", raw, voltage);
    return voltage;
}


// ---- Buzzer ----

void buzzerBeep(int count, int duration_ms) {
    for (int i = 0; i < count; i++) {
        digitalWrite(BUZZER_PIN, HIGH);
        delay(duration_ms);
        digitalWrite(BUZZER_PIN, LOW);
        if (i < count - 1) delay(duration_ms);
    }
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


// Render a short two-line failure message to the panel so the user can
// see *why* nothing refreshed without plugging into serial. Draws via
// the same Seeed_GFX epaper instance as displayImage(); no PSRAM
// allocation, no long busy loop. If anything throws or the GFX call
// stack misbehaves on a panel that didn't init cleanly, the caller
// still falls back to the buzzer + LED indication.
void showErrorFrame(const char* headline, const char* detail) {
    Serial.printf("[Display] Error frame: %s — %s\n",
                  headline ? headline : "(null)",
                  detail   ? detail   : "");

    epaper.fillScreen(TFT_WHITE);
    epaper.setTextColor(TFT_BLACK, TFT_WHITE);
    epaper.setTextDatum(MC_DATUM);

    epaper.setTextSize(6);
    epaper.drawString(headline ? headline : "Error",
                      DISPLAY_WIDTH / 2,
                      DISPLAY_HEIGHT / 2 - 40);

    if (detail && detail[0]) {
        epaper.setTextSize(3);
        epaper.drawString(detail,
                          DISPLAY_WIDTH / 2,
                          DISPLAY_HEIGHT / 2 + 40);
    }

    epaper.update();
}


// On Wi-Fi / server-fetch failure the panel keeps yesterday's content with
// no on-screen sign anything's wrong. Stamp an "OFFLINE" marker in the
// bottom-right corner so the user can tell at a glance the panel is showing
// stale, last-known content rather than today's.
void showErrorFrame() {
    epaper.setTextColor(TFT_BLACK, TFT_WHITE);
    epaper.setTextSize(1);
    epaper.fillRect(DISPLAY_WIDTH - 60, DISPLAY_HEIGHT - 16, 50, 12, TFT_WHITE);
    epaper.drawString("OFFLINE", DISPLAY_WIDTH - 58, DISPLAY_HEIGHT - 14);
    epaper.update();
}


// ---- Warm window ----

void warmWindow() {
    Serial.printf("[Warm] Watching buttons for %d ms\n", WARM_WINDOW_MS);
    unsigned long deadline = millis() + WARM_WINDOW_MS;
    while ((long)(deadline - millis()) > 0) {
        delay(20);
        int hit = -1;
        if (digitalRead(BTN_NEXT) == LOW)         hit = BTN_NEXT;
        else if (digitalRead(BTN_PREV) == LOW)    hit = BTN_PREV;
        else if (digitalRead(BTN_REFRESH) == LOW) hit = BTN_REFRESH;
        if (hit < 0) continue;

        delay(BTN_DEBOUNCE_MS);
        if (digitalRead(hit) != LOW) continue;  // bounced

        bool isLong = waitForLongPress(hit, LONG_PRESS_MS);

        if (hit == BTN_REFRESH)        handleRefresh(isLong);
        else if (hit == BTN_NEXT)      handlePageChange(isLong ? "last"  : "next");
        else                           handlePageChange(isLong ? "first" : "prev");

        while (digitalRead(hit) == LOW) delay(10);

        deadline = millis() + WARM_WINDOW_MS;
    }
    Serial.println("[Warm] Window closed");
}


// Returns true if the button is still held LOW after thresholdMs, false
// if released earlier. Short beep cue on long-press recognition so the
// user knows the gesture registered before the screen catches up.
bool waitForLongPress(int btnPin, int thresholdMs) {
    unsigned long start = millis();
    while ((long)(millis() - start) < thresholdMs) {
        if (digitalRead(btnPin) != LOW) return false;
        delay(20);
    }
    buzzerBeep(2, 30);
    return true;
}


// ---- Sleep ----

// Pick the sleep duration: server-provided next_wake_in_s when present
// and sane, otherwise the 24-h fallback. The bounds are a sanity check
// against a clock-skewed server (e.g. a value of 1 s would burn the
// battery; a value of 30 days would silently kill the device).
uint64_t computeSleepSeconds() {
    if (nextWakeInS < (int32_t)MIN_SLEEP_S || nextWakeInS > (int32_t)MAX_SLEEP_S) {
        return DAILY_REFRESH_INTERVAL_S;
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
