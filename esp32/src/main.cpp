/**
 * ePepper firmware — XIAO ESP32-S3 + reTerminal E1001
 *
 * Buttons (active-low, hw 10K pull-up + 100nF debounce):
 *   KEY0 (GPIO3)  — Refresh: force poll server for new content
 *   KEY1 (GPIO4)  — Next page
 *   KEY2 (GPIO5)  — Previous page
 *
 * Wake sources:
 *   - Timer: CLOCK_INTERVAL_S for clock overlay, POLL_INTERVAL_S for server poll
 *   - Any button press via ext1 bitmask (GPIO 3, 4, 5)
 *
 * Loop:
 *   1. Wake from deep sleep
 *   2. Read buttons to determine intent (refresh / next / prev)
 *   3. If button:
 *      a. Refresh → connect WiFi, poll server, fetch image if changed
 *      b. Next/Prev → connect WiFi, call /page/next or /page/prev, fetch image
 *   4. If timer:
 *      a. Every CLOCK_INTERVAL_S → update clock overlay (partial refresh, no WiFi)
 *      b. Every POLL_INTERVAL_S → connect WiFi, poll server for new recipe
 *   5. Deep sleep
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <time.h>
#include "TFT_eSPI.h"
#include "config.h"

EPaper epaper;

// ---- Types ----
enum WakeAction { WAKE_TIMER, WAKE_REFRESH, WAKE_NEXT, WAKE_PREV };

// ---- Forward declarations ----
void handleRefresh();
void handlePageChange(const char* direction);
void handleTimerWake();
void warmWindow();
void connectWiFi();
bool pollServer();
bool downloadImage(int page);
int requestPageChange(const char* direction);
void displayImage(uint8_t* data, size_t len);
void drawClockOverlay();
void drawClockContent();
void syncNTPIfStale();
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
RTC_DATA_ATTR int lastFullRefreshWake = 0;  // wakeCount of the last full-panel refresh; used to throttle anti-ghost full refreshes
RTC_DATA_ATTR char currentLang[3] = "en";   // recipe language (ISO 639-1), localizes the date overlay
RTC_DATA_ATTR float lastBatteryV = 3.7f;    // most recent battery voltage, refreshed during reportDeviceStatus

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

    epaper.begin();

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

    // After a button press, hold awake (and WiFi connected) for a warm window
    // so quick follow-up presses skip the cold-start + WiFi reconnect penalty.
    if (action != WAKE_TIMER) {
        warmWindow();
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

    syncNTPIfStale();

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

    syncNTPIfStale();

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
    digitalWrite(LED_PIN, HIGH);
}


void handleTimerWake() {
    bool needsServerPoll = (wakeCount % (POLL_INTERVAL_S / CLOCK_INTERVAL_S) == 0);
    int fullRefreshWakes = FULL_REFRESH_INTERVAL_S / CLOCK_INTERVAL_S;
    bool needsAntiGhostRefresh = hasContent && (wakeCount - lastFullRefreshWake) >= fullRefreshWakes;

    if (needsServerPoll || needsAntiGhostRefresh) {
        Serial.printf("[Timer] %s%s\n",
                      needsServerPoll ? "server poll" : "",
                      needsAntiGhostRefresh ? " + anti-ghost full refresh" : "");
        connectWiFi();
        if (WiFi.status() == WL_CONNECTED) {
            syncNTPIfStale();

            bool changed = pollServer();
            if (changed || needsAntiGhostRefresh) {
                if (downloadImage(currentPage)) {
                    displayImage(imageBuffer, imageSize);
                    Serial.println("[Timer] Full refresh complete");
                }
            }
            reportDeviceStatus();
        }
    }

    // Tick the clock overlay via partial refresh — but skip if we just did a
    // full refresh, which already painted the clock in the same pass.
    if (!needsAntiGhostRefresh) {
        drawClockOverlay();
    }
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

    const char* lang = doc["lang"] | "en";
    strncpy(currentLang, lang, sizeof(currentLang) - 1);
    currentLang[sizeof(currentLang) - 1] = '\0';

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
    lastBatteryV = battV;
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

void syncNTPIfStale() {
    time_t now;
    time(&now);
    if (lastNTPSync != 0 && now - lastNTPSync < NTP_SYNC_INTERVAL_S) return;
    syncNTP();
    lastNTPSync = now;
}


void syncNTP() {
    Serial.println("[NTP] Syncing...");
    // Europe/Zurich: CET (UTC+1) / CEST (UTC+2), DST last Sun of Mar→Oct
    configTzTime("CET-1CEST,M3.5.0,M10.5.0/3", "pool.ntp.org", "time.nist.gov");

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

// Localized weekday/month names. ASCII-only (Font 2 is ASCII 32-127); diacritics
// are dropped from Italian/Spanish, German uses the unaccented forms it doesn't
// need diacritics for anyway. Indexed by tm_wday (0=Sun) and tm_mon (0=Jan).
static const char* WEEKDAYS_EN[] = {"Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"};
static const char* WEEKDAYS_DE[] = {"Sonntag","Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag"};
static const char* WEEKDAYS_FR[] = {"dimanche","lundi","mardi","mercredi","jeudi","vendredi","samedi"};
static const char* WEEKDAYS_IT[] = {"domenica","lunedi","martedi","mercoledi","giovedi","venerdi","sabato"};
static const char* WEEKDAYS_ES[] = {"domingo","lunes","martes","miercoles","jueves","viernes","sabado"};
static const char* WEEKDAYS_NL[] = {"zondag","maandag","dinsdag","woensdag","donderdag","vrijdag","zaterdag"};

static const char* MONTHS_EN[] = {"January","February","March","April","May","June","July","August","September","October","November","December"};
static const char* MONTHS_DE[] = {"Januar","Februar","Maerz","April","Mai","Juni","Juli","August","September","Oktober","November","Dezember"};
static const char* MONTHS_FR[] = {"janvier","fevrier","mars","avril","mai","juin","juillet","aout","septembre","octobre","novembre","decembre"};
static const char* MONTHS_IT[] = {"gennaio","febbraio","marzo","aprile","maggio","giugno","luglio","agosto","settembre","ottobre","novembre","dicembre"};
static const char* MONTHS_ES[] = {"enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"};
static const char* MONTHS_NL[] = {"januari","februari","maart","april","mei","juni","juli","augustus","september","oktober","november","december"};


static const char* enOrdinal(int day) {
    if (day >= 11 && day <= 13) return "th";
    switch (day % 10) {
        case 1: return "st";
        case 2: return "nd";
        case 3: return "rd";
        default: return "th";
    }
}


static void formatDate(char* out, size_t outLen, const struct tm& t, const char* lang) {
    int wd = t.tm_wday, mo = t.tm_mon, md = t.tm_mday;
    if (strcmp(lang, "de") == 0) {
        snprintf(out, outLen, "%s, %d. %s", WEEKDAYS_DE[wd], md, MONTHS_DE[mo]);
    } else if (strcmp(lang, "fr") == 0) {
        snprintf(out, outLen, "%s %d %s", WEEKDAYS_FR[wd], md, MONTHS_FR[mo]);
    } else if (strcmp(lang, "it") == 0) {
        snprintf(out, outLen, "%s %d %s", WEEKDAYS_IT[wd], md, MONTHS_IT[mo]);
    } else if (strcmp(lang, "es") == 0) {
        snprintf(out, outLen, "%s %d de %s", WEEKDAYS_ES[wd], md, MONTHS_ES[mo]);
    } else if (strcmp(lang, "nl") == 0) {
        snprintf(out, outLen, "%s %d %s", WEEKDAYS_NL[wd], md, MONTHS_NL[mo]);
    } else {
        snprintf(out, outLen, "%s %s %d%s", WEEKDAYS_EN[wd], MONTHS_EN[mo], md, enOrdinal(md));
    }
}


static int batteryPercent(float v) {
    // Li-Po linear approximation: 4.20 V = 100 %, 3.00 V = 0 %.
    if (v >= 4.20f) return 100;
    if (v <= 3.00f) return 0;
    return (int)((v - 3.00f) / 1.20f * 100.0f);
}


void drawClockContent() {
    epaper.fillRect(CLOCK_RECT_X, CLOCK_RECT_Y, CLOCK_RECT_W, CLOCK_RECT_H, TFT_WHITE);

    struct tm timeinfo;
    if (!getLocalTime(&timeinfo, 100)) {
        Serial.println("[Display] Clock: no time yet");
        return;
    }

    char dateStr[48];
    formatDate(dateStr, sizeof(dateStr), timeinfo, currentLang);

    char buf[80];
    snprintf(buf, sizeof(buf), "%02d:%02d  %s   ",
             timeinfo.tm_hour, timeinfo.tm_min, dateStr);

    epaper.setTextFont(2);
    epaper.setTextSize(1);
    epaper.setTextColor(TFT_BLACK, TFT_WHITE);
    epaper.drawString(buf, CLOCK_X, CLOCK_Y);

    // Battery glyph: 36x14 body with a 3x6 nub on the right, fill proportional to %.
    const int bodyW = 36, bodyH = 14;
    const int nubW  = 3,  nubH  = 6;
    int batX = CLOCK_X + epaper.textWidth(buf);
    int batY = CLOCK_Y + 1;

    epaper.drawRect(batX, batY, bodyW, bodyH, TFT_BLACK);
    epaper.fillRect(batX + bodyW, batY + (bodyH - nubH) / 2, nubW, nubH, TFT_BLACK);

    int pct = batteryPercent(lastBatteryV);
    int innerMax = bodyW - 4;
    int fillW = (innerMax * pct) / 100;
    if (fillW > 0) {
        epaper.fillRect(batX + 2, batY + 2, fillW, bodyH - 4, TFT_BLACK);
    }

    Serial.printf("[Display] Clock: %s [batt %.2fV %d%%]\n", buf, lastBatteryV, pct);
}


void drawClockOverlay() {
    drawClockContent();
    epaper.updataPartial(CLOCK_RECT_X, CLOCK_RECT_Y, CLOCK_RECT_W, CLOCK_RECT_H);
}


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

    // BMP is bottom-up by default — swap rows in place so drawBitmap renders right-side-up
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
    drawClockContent();
    epaper.update();
    lastFullRefreshWake = wakeCount;

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

        if (hit == BTN_REFRESH)   handleRefresh();
        else if (hit == BTN_NEXT) handlePageChange("next");
        else                      handlePageChange("prev");

        // Wait for release so we don't immediately re-trigger
        while (digitalRead(hit) == LOW) delay(10);

        deadline = millis() + WARM_WINDOW_MS;
    }
    Serial.println("[Warm] Window closed");
}


// ---- Sleep ----

void goToSleep(uint64_t seconds) {
    Serial.printf("[ePepper] Sleeping for %llu seconds...\n\n", seconds);

    if (WiFi.status() == WL_CONNECTED) {
        WiFi.disconnect(true);
    }

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
