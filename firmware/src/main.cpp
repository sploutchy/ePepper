/**
 * ePepper firmware — ESP32-S3 + reTerminal E1001
 *
 * Loop:
 *   1. Wake from deep sleep (timer or button press)
 *   2. If button press → connect WiFi, poll server, display, sleep
 *   3. If timer:
 *      a. Every CLOCK_INTERVAL_S  → update clock/temp (partial refresh, no WiFi)
 *      b. Every POLL_INTERVAL_S   → connect WiFi, poll server for new recipe
 *   4. Deep sleep
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <time.h>
#include "config.h"

// ---- Forward declarations ----
void connectWiFi();
bool pollServer();
bool downloadImage(int page);
void displayImage(const uint8_t* data, size_t len);
void drawStatusBar();
void updateClock();
void reportDeviceStatus();
void syncNTP();
void goToSleep(uint64_t seconds);

// ---- State ----
RTC_DATA_ATTR char lastHash[16] = "";
RTC_DATA_ATTR int currentPage = 1;
RTC_DATA_ATTR int totalPages = 1;
RTC_DATA_ATTR int wakeCount = 0;
RTC_DATA_ATTR time_t lastNTPSync = 0;
RTC_DATA_ATTR bool hasContent = false;

// Image buffer in PSRAM
uint8_t* imageBuffer = nullptr;
size_t imageSize = 0;

// Wake reason
bool wokeByButton = false;

void setup() {
    Serial.begin(115200);
    delay(100);

    wakeCount++;
    Serial.printf("\n[ePepper] Wake #%d\n", wakeCount);

    // Determine wake reason
    esp_sleep_wakeup_cause_t wakeReason = esp_sleep_get_wakeup_cause();
    wokeByButton = (wakeReason == ESP_SLEEP_WAKEUP_EXT0);

    if (wokeByButton) {
        Serial.println("[ePepper] Woke by button press");
    }

    // Init LED
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, HIGH); // OFF (active LOW)

    // Init button
    pinMode(BUTTON_PIN, INPUT_PULLUP);

    // Allocate image buffer in PSRAM
    imageBuffer = (uint8_t*)ps_malloc(DISPLAY_WIDTH * DISPLAY_HEIGHT / 8 + 1024);
    if (!imageBuffer) {
        Serial.println("[ePepper] ERROR: Failed to allocate PSRAM buffer");
        goToSleep(CLOCK_INTERVAL_S);
        return;
    }

    // TODO: Initialize display driver (Seeed_GFX)
    // This depends on the exact library setup. Placeholder for now.
    // display.begin();

    if (wokeByButton) {
        // Button press: force check server
        digitalWrite(LED_PIN, LOW); // LED on
        connectWiFi();
        if (WiFi.status() == WL_CONNECTED) {
            bool changed = pollServer();
            if (changed) {
                downloadImage(currentPage);
                // TODO: full refresh display with downloaded image
                Serial.println("[ePepper] New content displayed");
            } else {
                // Cycle to next page if multi-page
                if (totalPages > 1) {
                    currentPage = (currentPage % totalPages) + 1;
                    downloadImage(currentPage);
                    // TODO: full refresh display
                    Serial.printf("[ePepper] Page %d/%d\n", currentPage, totalPages);
                } else {
                    Serial.println("[ePepper] No changes");
                }
            }
            reportDeviceStatus();
            WiFi.disconnect(true);
        }
        digitalWrite(LED_PIN, HIGH); // LED off

    } else {
        // Timer wake
        bool needsServerPoll = (wakeCount % (POLL_INTERVAL_S / CLOCK_INTERVAL_S) == 0);

        if (needsServerPoll) {
            Serial.println("[ePepper] Server poll cycle");
            connectWiFi();
            if (WiFi.status() == WL_CONNECTED) {
                // Sync NTP if needed
                time_t now;
                time(&now);
                if (now - lastNTPSync > NTP_SYNC_INTERVAL_S || lastNTPSync == 0) {
                    syncNTP();
                    lastNTPSync = now;
                }

                bool changed = pollServer();
                if (changed) {
                    downloadImage(currentPage);
                    // TODO: full refresh display with new content
                    Serial.println("[ePepper] New content from server");
                }
                reportDeviceStatus();
                WiFi.disconnect(true);
            }
        }

        // Always update status bar (clock + temp) via partial refresh
        drawStatusBar();
    }

    // Sleep until next cycle
    goToSleep(CLOCK_INTERVAL_S);
}


void loop() {
    // Never reached — we use deep sleep
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


void reportDeviceStatus() {
    HTTPClient http;

    // Read battery voltage (ADC — platform specific, placeholder)
    int batteryMv = 0; // TODO: read actual battery voltage via ADC

    String url = String(SERVER_URL) + "/device/status"
                 + "?battery_mv=" + String(batteryMv)
                 + "&rssi=" + String(WiFi.RSSI())
                 + "&uptime_s=" + String(wakeCount * CLOCK_INTERVAL_S);
    http.begin(url);
    http.addHeader("Authorization", String("Bearer ") + API_KEY);

    int code = http.POST("");
    Serial.printf("[API] Device status reported: %d\n", code);
    http.end();
}


// ---- NTP ----

void syncNTP() {
    Serial.println("[NTP] Syncing time...");
    configTime(3600, 3600, "pool.ntp.org", "time.nist.gov");

    struct tm timeinfo;
    if (getLocalTime(&timeinfo, 5000)) {
        Serial.printf("[NTP] Time: %04d-%02d-%02d %02d:%02d:%02d\n",
                      timeinfo.tm_year + 1900, timeinfo.tm_mon + 1, timeinfo.tm_mday,
                      timeinfo.tm_hour, timeinfo.tm_min, timeinfo.tm_sec);
    } else {
        Serial.println("[NTP] Failed to get time");
    }
}


// ---- Display ----

void drawStatusBar() {
    // TODO: Implement with Seeed_GFX partial refresh
    // Draw time, date, temperature in the top STATUS_BAR_HEIGHT pixels
    //
    // Pseudocode:
    //   struct tm timeinfo;
    //   getLocalTime(&timeinfo);
    //   float temp = readTemperatureSensor();
    //
    //   display.setPartialWindow(0, 0, DISPLAY_WIDTH, STATUS_BAR_HEIGHT);
    //   display.fillRect(0, 0, DISPLAY_WIDTH, STATUS_BAR_HEIGHT, WHITE);
    //   display.setCursor(20, 15);
    //   display.setFont(&FreeSansBold18pt7b);
    //   display.printf("%02d:%02d", timeinfo.tm_hour, timeinfo.tm_min);
    //   display.setCursor(300, 15);
    //   display.printf("%s %d %s", dayStr, timeinfo.tm_mday, monthStr);
    //   display.setCursor(650, 15);
    //   display.printf("%.1f°C", temp);
    //   display.display(true); // partial refresh

    struct tm timeinfo;
    if (getLocalTime(&timeinfo, 100)) {
        Serial.printf("[Display] Status bar: %02d:%02d  %02d/%02d  ",
                      timeinfo.tm_hour, timeinfo.tm_min,
                      timeinfo.tm_mday, timeinfo.tm_mon + 1);
    }

    // TODO: Read temp/humidity from onboard sensor (I2C)
    // Wire.begin(I2C_SDA, I2C_SCL);
    // ... read sensor ...
    Serial.println("(display update placeholder)");
}


void displayImage(const uint8_t* data, size_t len) {
    // TODO: Parse BMP header, extract pixel data, push to display
    // The BMP from the server is 800x430 1-bit
    // Skip BMP header (typically 62 bytes for 1-bit BMP)
    // Feed raw pixel data to the display driver
    //
    // Pseudocode:
    //   uint32_t dataOffset = *(uint32_t*)(data + 10);  // BMP data offset
    //   const uint8_t* pixels = data + dataOffset;
    //   display.drawBitmap(0, STATUS_BAR_HEIGHT, pixels,
    //                      DISPLAY_WIDTH, DISPLAY_HEIGHT - STATUS_BAR_HEIGHT, BLACK);
    //   display.display(false); // full refresh

    Serial.printf("[Display] Would display %d bytes of image data\n", len);
}


// ---- Sleep ----

void goToSleep(uint64_t seconds) {
    Serial.printf("[ePepper] Sleeping for %llu seconds...\n\n", seconds);

    // Enable wake by button (GPIO3, active LOW)
    esp_sleep_enable_ext0_wakeup((gpio_num_t)BUTTON_PIN, 0);

    // Enable wake by timer
    esp_sleep_enable_timer_wakeup(seconds * 1000000ULL);

    // Free PSRAM buffer before sleep
    if (imageBuffer) {
        free(imageBuffer);
        imageBuffer = nullptr;
    }

    esp_deep_sleep_start();
}
