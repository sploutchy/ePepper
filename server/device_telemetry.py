"""Device telemetry — battery, RSSI, and last-seen heartbeat.

The ESP32 POSTs a wake-cycle report on every button press and on its
daily timer wake; this module holds the latest snapshot in memory
(no persistence — the next wake repopulates it) and decides when to fire
a one-shot low-battery alert.

Alert *delivery* (Telegram messages) lives in `bot/handlers.py` —
that's UI territory. This module just decides WHETHER to alert.
"""

import time
from typing import Any


# Device status (reported by ESP32 on every wake — button press or
# daily timer). Whichever fires more recently overwrites the others.
_device: dict[str, Any] = {
    "battery_mv": 0,
    "rssi": 0,
    "temperature_c": None,
    "humidity_pct": None,
    "firmware_version": None,
    "last_seen": 0,
}


# Fire a low-battery alert the first time a reading drops below
# LOW_BATTERY_MV, then re-arm only once it climbs back above
# LOW_BATTERY_REARM_MV. The 100 mV hysteresis band matters because the ADC
# reading is noisy (±tens of mV): a cell decaying through the threshold
# would otherwise straddle it for weeks, re-arming and re-alerting on
# alternating wakes. The flag is in-memory (reset on restart); at ~1-2
# reports/day a stray repeat alert after a redeploy is harmless.
LOW_BATTERY_MV = 3500
LOW_BATTERY_REARM_MV = 3600

_low_battery_alerted = False

# Heartbeat staleness threshold — the status views flag the device "overdue"
# when it hasn't reported in this long. Firmware reports on button press + a
# daily timer wake; 25 h gives the daily timer a buffer for clock drift / a
# slow Wi-Fi reconnect.
STALE_HEARTBEAT_S = 25 * 3600


def update_device_status(
    battery_mv: int,
    rssi: int,
    temperature_c: float | None = None,
    humidity_pct: float | None = None,
    firmware_version: int | None = None,
) -> dict:
    """Update device status from an ESP32 wake-cycle report.

    `temperature_c` / `humidity_pct` / `firmware_version` are optional so
    older firmware builds that don't yet report them keep working.

    Returns `{"low_battery_alert_mv": int | None}`. When non-None, the
    battery just crossed below LOW_BATTERY_MV and the caller is expected
    to deliver this alert (e.g. via Telegram). Fires once until the
    reading recovers above LOW_BATTERY_REARM_MV.
    """
    global _low_battery_alerted

    update = {
        "battery_mv": battery_mv,
        "rssi": rssi,
        "temperature_c": temperature_c,
        "humidity_pct": humidity_pct,
        "last_seen": int(time.time()),
    }
    # Preserve the previously-reported version when this POST omits the
    # field — a pre-OTA firmware build wouldn't send it, and we don't
    # want a single stale POST to blank out a known value.
    if firmware_version is not None:
        update["firmware_version"] = firmware_version
    _device.update(update)

    alert_mv: int | None = None
    if battery_mv > 0:
        if battery_mv < LOW_BATTERY_MV:
            if not _low_battery_alerted:
                _low_battery_alerted = True
                alert_mv = battery_mv
        elif battery_mv >= LOW_BATTERY_REARM_MV:
            # Only re-arm above the hysteresis band — readings inside
            # [LOW_BATTERY_MV, LOW_BATTERY_REARM_MV) keep the current state
            # so ADC noise around the threshold can't re-trigger the alert.
            _low_battery_alerted = False

    return {"low_battery_alert_mv": alert_mv}


def get_device_status() -> dict:
    """Get last known device status."""
    return dict(_device)
