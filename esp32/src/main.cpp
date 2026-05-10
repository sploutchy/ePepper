/**
 * ePepper firmware — XIAO ESP32-S3 + reTerminal E1001
 *
 * Buttons (active-low, hw 10K pull-up + 100nF debounce):
 *   KEY0 (GPIO3)  — Refresh: force poll server for new content
 *   KEY1 (GPIO4)  — Next page
 *   KEY2 (GPIO5)  — Previous page
 *
 * Wake sources:
 *   - Timer: CLOCK_INTERVAL_S for status bar, POLL_INTERVAL_S for server poll
 *   - Any button press via ext1 bitmask (GPIO 3, 4, 5)
 *
 * Loop:
 *   1. Wake from deep sleep
 *   2. Read buttons to determine intent (refresh / next / prev)
 *   3. If button:
 *      a. Refresh → connect WiFi, poll server, fetch image if changed
 *      b. Next/Prev → connect WiFi, call /page/next or /page/prev, fetch image
 *   4. If timer:
 *      a. Every CLOCK_INTERVAL_S → update status bar (partial refresh, no WiFi)
 *      b. Every POLL_INTERVAL_S → connect WiFi, poll server for new recipe
 *   5. Deep sleep
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <time.h>
#include "config.h"

// ---- Types ----
enum WakeAction { WAKE_TIMER, WAKE_REFRESH, WAKE_NEXT, WAKE_PREV };

// ---- Forward declarations ----
void connectWiFi();
bool pollServer();
bool downloadImage(int page);
int requestPageChange(const char* direction);
void displayImage(const uint8_t* data, size_t len);
void drawStatusBar();
void reportDeviceStatus();
void syncNTP();
void buzzerBeep(int count, int duration_ms);
float readBatteryVoltage();
void goToSleep(uint64_t seconds);
WakeAction detectWakeAction();

// ---- RTC-persistent state (survives deep sleep) ----
RTC_DATA_ATTR char lastHash[16] = "";
RTC_DATA_ATTR int currentPage = 1;
RTC_DATA_ATTR int totalPages = 1;
RTC_DATA_ATTR int wakeCount = 0;
RTC_DATA_ATTR time_t lastNTPSync = 0;
RTC_DATA_ATTR bool hasContent = false;

// ---- Transient state ----
uint8_t* imageBuffer = nullptr;
size_t imageSize = 0;


void setup() {
    Serial.begin(115200);
    delay(100);

    wakeCount++;
    Serial.printf("\n[ePepper] Wake #%d\n", wakeCount);

    // Init outputs
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, HIGH);  // LED off (active-low)
    pinMode(BUZZER_PIN, OUTPUT);
    digitalWrite(BUZZER_PIN, LOW);  // Buzzer off (active-high)

    // Init buttons (hw pull-ups on board, just set as input)
    pinMode(BTN_REFRESH, INPUT);
    pinMode(BTN_NEXT, INPUT);
    pinMode(BTN_PREV, INPUT);

    // Allocate image buffer in PSRAM
    imageBuffer = (uint8_t*)ps_malloc(DISPLAY_WIDTH * DISPLAY_HEIGHT / 8 + 1024);
    if (!imageBuffer) {
        Serial.println("[ePepper] ERROR: Failed to allocate PSRAM buffer");
        goToSleep(CLOCK_INTERVAL_S);
        return;
    }

    // TODO: Initialize e-paper display driver
    // display.begin();

    WakeAction action = detectWakeAction();

    switch (action) {
        case WAKE_REFRESH:
            handleRefresh();
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

    goToSleep(CLOCK_INTERVAL_S);
}


void loop() {
    // Never reached — we use deep sleep
}


// ---- Wake detection ----

WakeAction detectWakeAction() {
    esp_sleep_wakeup_cause_t reason = esp_sleep_get_wakeup_cause();
    if (reason != ESP_SLEEP_WAKEUP_EXT1) {
        Serial.println("[Wake] Timer");
        return WAKE_TIMER;
    }

    // ext1: check which GPIO triggered the wake
    uint64_t mask = esp_sleep_get_ext1_wakeup_status();
    if (mask & (1ULL << BTN_NEXT)) {
        Serial.println("[Wake] Button: Next");
        return WAKE_NEXT;
    }
    if (mask & (1ULL << BTN_PREV)) {
        Serial.println("[Wake] Button: Prev");
        return WAKE_PREV;
    }
    // Default to refresh (KEY0 or unknown)
    Serial.println("[Wake] Button: Refresh");
    return WAKE_REFRESH;
}


// ---- Button handlers ----

void handleRefresh() {
    Serial.println("[Action] Refresh — polling server");
    digitalWrite(LED_PIN, LOW);  // LED on
    buzzerBeep(1, 50);

    connectWiFi();
    if (WiFi.status() != WL_CONNECTED) {
        buzzerBeep(3, 100);  // Error beep
        digitalWrite(LED_PIN, HIGH);
        return;
    }

    bool changed = pollServer();
    if (changed) {
        if (downloadImage(currentPage)) {
            displayImage(imageBuffer, imageSize);
            Serial.println("[Action] New content displayed");
        }
    } else {
        Serial.println("[Action] No changes");
    }

    reportDeviceStatus();
    WiFi.disconnect(true);
    digitalWrite(LED_PIN, HIGH);
}


void handlePageChange(const char* direction) {
    Serial.printf("[Action] Page %s\n", direction);
    digitalWrite(LED_PIN, LOW);

    if (totalPages <= 1) {
        Serial.println("[Action] Single page, nothing to do");
        buzzerBeep(2, 50);  // Two short beeps = can't navigate
        digitalWrite(LED_PIN, HIGH);
        return;
    }

    connectWiFi();
    if (WiFi.status() != WL_CONNECTED) {
        buzzerBeep(3, 100);
        digitalWrite(LED_PIN, HIGH);
        return;
    }

    int newPage = requestPageChange(direction);
    if (newPage > 0 && newPage != currentPage) {
        currentPage = newPage;
        if (downloadImage(currentPage)) {
            displayImage(imageBuffer, imageSize);
            buzzerBeep(1, 30);
            Serial.printf("[Action] Page %d/%d displayed\n", currentPage, totalPages);
        }
    }

    reportDeviceStatus();
    WiFi.disconnect(true);
    digitalWrite(LED_PIN, HIGH);
}


void handleTimerWake() {
    bool needsServerPoll = (wakeCount % (POLL_INTERVAL_S / CLOCK_INTERVAL_S) == 0);

    if (needsServerPoll) {
        Serial.println("[Timer] Server poll cycle");
        connectWiFi();
        if (WiFi.status() == WL_CONNECTED) {
            // Sync NTP if stale
            time_t now;
            time(&now);
            if (now - lastNTPSync > NTP_SYNC_INTERVAL_S || lastNTPSync == 0) {
                syncNTP();
                lastNTPSync = now;
            }

            bool changed = pollServer();
            if (changed) {
                if (downloadImage(currentPage)) {
                    displayImage(imageBuffer, imageSize);
                    Serial.println("[Timer] New content from server");
                }
            }
            reportDeviceStatus();
            WiFi.disconnect(true);
        }
    }

    // Always update status bar (clock + temp) via partial refresh
    drawStatusBar();
}


// ---- WiFi ----

void connectWiFi() {
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

    int code = http.GET();
    if (code != 200) {
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

    const char* hash = doc["hash"];
    totalPages = doc["total_pages"] | 1;
    currentPage = doc["page"] | 1;

    if (hash && strcmp(hash, lastHash) != 0) {
        strncpy(lastHash, hash, sizeof(lastHash) - 1);
        hasContent = true;
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

    int code = http.POST("");
    if (code != 200) {
        Serial.printf("[API] /page/%s returned %d\n", direction, code);
        http.end();
        return -1;
    }

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
    HTTPClient http;

    // Enable battery ADC, read, disable
    float battV = readBatteryVoltage();
    int batteryMv = (int)(battV * 1000);

    String url = String(SERVER_URL) + "/device/status"
                 + "?battery_mv=" + String(batteryMv)
                 + "&rssi=" + String(WiFi.RSSI())
                 + "&uptime_s=" + String(wakeCount * CLOCK_INTERVAL_S);
    http.begin(url);
    http.addHeader("Authorization", String("Bearer ") + API_KEY);

    int code = http.POST("");
    Serial.printf("[API] Device status: battery=%dmV rssi=%d → %d\n",
                  batteryMv, WiFi.RSSI(), code);
    http.end();
}


// ---- Battery ----

float readBatteryVoltage() {
    // Enable battery voltage divider
    pinMode(BATTERY_ENABLE_PIN, OUTPUT);
    digitalWrite(BATTERY_ENABLE_PIN, HIGH);
    delay(10);  // Let ADC settle

    int raw = analogRead(BATTERY_ADC_PIN);

    // Disable to save power
    digitalWrite(BATTERY_ENABLE_PIN, LOW);

    // ESP32-S3 ADC: 12-bit (0-4095), 0-3.3V range
    // Battery voltage divider ratio depends on board design (typically 2:1)
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
        if (i < count - 1) delay(duration_ms);  // Gap between beeps
    }
}


// ---- NTP ----

void syncNTP() {
    Serial.println("[NTP] Syncing...");
    configTime(3600, 3600, "pool.ntp.org", "time.nist.gov");

    struct tm timeinfo;
    if (getLocalTime(&timeinfo, 5000)) {
        Serial.printf("[NTP] %04d-%02d-%02d %02d:%02d:%02d\n",
                      timeinfo.tm_year + 1900, timeinfo.tm_mon + 1, timeinfo.tm_mday,
                      timeinfo.tm_hour, timeinfo.tm_min, timeinfo.tm_sec);
    } else {
        Serial.println("[NTP] Failed");
    }
}


// ---- Display ----

void drawStatusBar() {
    // TODO: Implement with e-paper partial refresh
    // Draw time + date + temperature in the top STATUS_BAR_HEIGHT pixels
    //
    //   struct tm timeinfo;
    //   getLocalTime(&timeinfo);
    //   float temp = readSHT40Temperature();
    //
    //   display.setPartialWindow(0, 0, DISPLAY_WIDTH, STATUS_BAR_HEIGHT);
    //   display.fillRect(0, 0, DISPLAY_WIDTH, STATUS_BAR_HEIGHT, WHITE);
    //   display.printf("%02d:%02d", timeinfo.tm_hour, timeinfo.tm_min);
    //   display.printf("%.1f°C", temp);
    //   display.display(true);  // partial refresh

    struct tm timeinfo;
    if (getLocalTime(&timeinfo, 100)) {
        Serial.printf("[Display] Status bar: %02d:%02d %02d/%02d (placeholder)\n",
                      timeinfo.tm_hour, timeinfo.tm_min,
                      timeinfo.tm_mday, timeinfo.tm_mon + 1);
    }
}


void displayImage(const uint8_t* data, size_t len) {
    // TODO: Parse BMP header, push pixel data to e-paper
    //
    //   uint32_t dataOffset = *(uint32_t*)(data + 10);  // BMP pixel data offset
    //   const uint8_t* pixels = data + dataOffset;
    //   display.drawBitmap(0, STATUS_BAR_HEIGHT, pixels,
    //                      DISPLAY_WIDTH, DISPLAY_HEIGHT - STATUS_BAR_HEIGHT, BLACK);
    //   display.display(false);  // full refresh

    Serial.printf("[Display] Would display %d bytes of image data (placeholder)\n", len);
}


// ---- Sleep ----

void goToSleep(uint64_t seconds) {
    Serial.printf("[ePepper] Sleeping for %llu seconds...\n\n", seconds);

    // Wake on any of the 3 buttons (ext1, active-low → wake on LOW)
    uint64_t buttonMask = (1ULL << BTN_REFRESH) | (1ULL << BTN_NEXT) | (1ULL << BTN_PREV);
    esp_sleep_enable_ext1_wakeup(buttonMask, ESP_EXT1_WAKEUP_ANY_LOW);

    // Wake on timer
    esp_sleep_enable_timer_wakeup(seconds * 1000000ULL);

    // Free PSRAM before sleep
    if (imageBuffer) {
        free(imageBuffer);
        imageBuffer = nullptr;
    }

    esp_deep_sleep_start();
}
