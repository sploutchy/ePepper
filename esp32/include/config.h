/**
 * ePepper firmware configuration.
 *
 * Edit these values before flashing, or they will be overwritten
 * by WiFi provisioning on first boot.
 */

#ifndef EPEPPER_CONFIG_H
#define EPEPPER_CONFIG_H

// ----- WiFi -----
#define WIFI_SSID         "YOUR_WIFI_SSID"
#define WIFI_PASSWORD     "YOUR_WIFI_PASSWORD"
#define WIFI_TIMEOUT_MS   15000

// ----- Server -----
#define SERVER_URL        "https://epepper.fyx.ch"
#define API_KEY           "CHANGE_ME"  // generate with: python3 -c "import secrets; print(secrets.token_urlsafe(32))"

// ----- Display -----
// reTerminal E1001: 7.5" mono, UC8179, 800x480
#define DISPLAY_WIDTH     800
#define DISPLAY_HEIGHT    480
#define STATUS_BAR_HEIGHT 50

// ----- Timing -----
#define POLL_INTERVAL_S       300   // check server every 5 minutes
#define CLOCK_INTERVAL_S      60    // update clock every 1 minute
#define NTP_SYNC_INTERVAL_S   3600  // sync NTP every hour

// ----- Pins (reTerminal E1001) -----
#define EPD_CLK   7
#define EPD_MOSI  9
#define EPD_CS    10
#define EPD_DC    11
#define EPD_RST   12
#define EPD_BUSY  13

// Status LED (active LOW)
#define LED_PIN   6

// Buttons (active LOW, hardware pull-ups)
#define BTN_REFRESH  3   // KEY0 — right (green), refresh display
#define BTN_NEXT     4   // KEY1 — middle, next page
#define BTN_PREV     5   // KEY2 — left, previous page
#define BTN_DEBOUNCE_MS 50

// I2C for SHT4x temp/humidity sensor
#define I2C_SDA   19
#define I2C_SCL   20

// Buzzer (piezo)
#define BUZZER_PIN 45

// Battery ADC
#define BATTERY_ADC_PIN    1
#define BATTERY_ENABLE_PIN 21

#endif // EPEPPER_CONFIG_H
