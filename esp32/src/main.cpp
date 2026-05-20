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
 *   2. WiFi up
 *   3. Read battery + SHT40, POST /device/status — server uses the fresh
 *      battery to bake the on-screen glyph into the next render
 *   4. Refresh → /version hash check, /image if changed (or always if forced)
 *      Page nav → /page/<dir>, /image
 *      Timer → same as refresh, not forced
 *   5. Display BMP (always full refresh — server owns every pixel)
 *   6. Deep sleep
 *
 * Time is set from the HTTP Date header in every server response, so we
 * never call out to NTP. Logs stay roughly correct as a side effect.
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <sys/time.h>
#include <time.h>
#include "TFT_eSPI.h"
#include "EPaperFixed.h"  // Kept dormant: subclass adds partial-refresh override we no longer call.
#include "config.h"

EPaperFixed epaper;

enum WakeAction { WAKE_TIMER, WAKE_REFRESH, WAKE_NEXT, WAKE_PREV, WAKE_CLEAR };

void handleRefresh(bool force = false);
void handlePageChange(const char* direction);
void handleClear();
void handleTimerWake();
void warmWindow();
bool waitForLongPress(int btnPin, int thresholdMs);
void connectWiFi();
bool pollServer();
bool downloadImage(int page);
int requestPageChange(const char* direction);
void reportDeviceStatus();
void displayImage(uint8_t* data, size_t len);
bool readSHT40(float& tempC, float& rh);
float readBatteryVoltage();
void buzzerBeep(int count, int duration_ms);
void goToSleep(uint64_t seconds);
WakeAction detectWakeAction();
void collectDateHeader(HTTPClient& http);
void applyDateHeader(HTTPClient& http);
uint64_t computeSleepSeconds();

// ---- RTC-persistent state (survives deep sleep) ----
RTC_DATA_ATTR char lastHash[16] = "";
RTC_DATA_ATTR int currentPage = 1;
RTC_DATA_ATTR int totalPages = 1;
RTC_DATA_ATTR int wakeCount = 0;

// ---- Transient state ----
uint8_t* imageBuffer = nullptr;
size_t imageSize = 0;

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
        buzzerBeep(3, 100);
        digitalWrite(LED_PIN, HIGH);
        return;
    }

    reportDeviceStatus();

    bool changed = pollServer();
    if (changed || force) {
        if (downloadImage(currentPage)) {
            displayImage(imageBuffer, imageSize);
            Serial.println(force ? "[Action] Forced full redraw"
                                 : "[Action] New content displayed");
        }
    } else {
        Serial.println("[Action] No changes");
    }

    digitalWrite(LED_PIN, HIGH);
}


void handlePageChange(const char* direction) {
    Serial.printf("[Action] Page %s\n", direction);
    digitalWrite(LED_PIN, LOW);

    if (totalPages <= 1) {
        Serial.println("[Action] Single page, nothing to do");
        buzzerBeep(2, 50);
        digitalWrite(LED_PIN, HIGH);
        return;
    }

    connectWiFi();
    if (WiFi.status() != WL_CONNECTED) {
        buzzerBeep(3, 100);
        digitalWrite(LED_PIN, HIGH);
        return;
    }

    reportDeviceStatus();

    int newPage = requestPageChange(direction);
    if (newPage > 0 && newPage != currentPage) {
        currentPage = newPage;
        if (downloadImage(currentPage)) {
            displayImage(imageBuffer, imageSize);
            buzzerBeep(1, 30);
            Serial.printf("[Action] Page %d/%d displayed\n", currentPage, totalPages);
        }
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
        buzzerBeep(3, 100);
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
        buzzerBeep(3, 100);
        digitalWrite(LED_PIN, HIGH);
        return;
    }

    currentPage = 1;
    totalPages = 1;
    if (downloadImage(1)) {
        displayImage(imageBuffer, imageSize);
        Serial.println("[Action] Cleared display");
    }

    digitalWrite(LED_PIN, HIGH);
}


// Daily ambient pull — server's anniversary scheduler may have rotated
// content in at midnight. Same wire shape as refresh, just unforced.
void handleTimerWake() {
    Serial.println("[Timer] Daily wake — checking for ambient content");
    handleRefresh(false);
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

    const char* hash = doc["hash"];
    totalPages = doc["total_pages"] | 1;
    currentPage = doc["page"] | 1;
    // Server tells us when to next wake. Missing on old servers → -1
    // → fallback to DAILY_REFRESH_INTERVAL_S in computeSleepSeconds.
    nextWakeInS = doc["next_wake_in_s"] | -1;

    if (hash && strcmp(hash, lastHash) != 0) {
        strncpy(lastHash, hash, sizeof(lastHash) - 1);
        Serial.printf("[API] New content: hash=%s pages=%d\n", hash, totalPages);
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
    while (http.connected() && bytesRead < imageSize) {
        size_t available = stream->available();
        if (available) {
            size_t toRead = min(available, imageSize - bytesRead);
            stream->readBytes(imageBuffer + bytesRead, toRead);
            bytesRead += toRead;
        }
        delay(1);
    }

    http.end();
    Serial.printf("[API] Downloaded %d bytes\n", bytesRead);
    return bytesRead == imageSize;
}


int requestPageChange(const char* direction) {
    HTTPClient http;
    String url = String(SERVER_URL) + "/page/" + direction;
    http.begin(url);
    http.addHeader("Authorization", String("Bearer ") + API_KEY);
    collectDateHeader(http);

    int code = http.POST("");
    if (code != 200) {
        Serial.printf("[API] /page/%s returned %d\n", direction, code);
        http.end();
        return -1;
    }
    applyDateHeader(http);

    String body = http.getString();
    http.end();

    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, body);
    if (err) {
        Serial.printf("[API] JSON parse error: %s\n", err.c_str());
        return -1;
    }

    bool ok = doc["ok"] | false;
    if (!ok) {
        Serial.printf("[API] /page/%s: no change\n", direction);
        return -1;
    }

    int newPage = doc["page"] | 1;
    totalPages = doc["total_pages"] | 1;
    Serial.printf("[API] Page %s → %d/%d\n", direction, newPage, totalPages);
    return newPage;
}


void reportDeviceStatus() {
    float battV = readBatteryVoltage();
    int batteryMv = (int)(battV * 1000);

    float tempC = 0, rh = 0;
    bool envOk = readSHT40(tempC, rh);

    String url = String(SERVER_URL) + "/device/status"
                 + "?battery_mv=" + String(batteryMv)
                 + "&rssi=" + String(WiFi.RSSI());
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
