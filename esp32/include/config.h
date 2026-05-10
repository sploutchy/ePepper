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

// Button (user button for force refresh)
#define BUTTON_PIN 3

// I2C for temp/humidity sensor
#define I2C_SDA   19
#define I2C_SCL   20

// Buzzer
#define BUZZER_PIN 45

#endif // EPEPPER_CONFIG_H
