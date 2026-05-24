"""Device telemetry — battery, RSSI, heartbeat, and alert hysteresis.

The ESP32 POSTs a wake-cycle report on every button press and on its
daily timer wake; this module holds the latest snapshot in memory
(no persistence — the next wake repopulates it) and owns the
low-battery / stale-heartbeat alert hysteresis so the bot only fires
each alert once per episode.

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


# Low-battery alert thresholds. Cross BELOW LOW_BATTERY_MV → fire an alert
# once; only re-arm after the reading climbs back above LOW_BATTERY_MV +
# HYSTERESIS to avoid repeated alerts on a noisy reading near the boundary.
LOW_BATTERY_MV = 3500
LOW_BATTERY_HYSTERESIS_MV = 100

_low_battery_alerted = False

# Heartbeat staleness. Firmware reports on button press + a daily timer wake;
# 25 h gives the daily timer a buffer for clock drift / a slow Wi-Fi reconnect.
# Alert once when crossed; re-arm only when the next POST arrives (handled in
# update_device_status). The check itself runs proactively from scheduler.py
# because the absence of POSTs is exactly what we're detecting.
STALE_HEARTBEAT_S = 25 * 3600

_stale_heartbeat_alerted = False


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
    battery just crossed below the threshold and the caller is expected
    to deliver this alert (e.g. via Telegram). Hysteresis prevents the
    alert from firing again until the battery climbs above
    LOW_BATTERY_MV + LOW_BATTERY_HYSTERESIS_MV.
    """
    global _low_battery_alerted, _stale_heartbeat_alerted

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

    # Fresh POST means the device is back — re-arm the staleness alert.
    _stale_heartbeat_alerted = False

    alert_mv: int | None = None
    if battery_mv > 0:
        if battery_mv < LOW_BATTERY_MV and not _low_battery_alerted:
            _low_battery_alerted = True
            alert_mv = battery_mv
        elif battery_mv > LOW_BATTERY_MV + LOW_BATTERY_HYSTERESIS_MV:
            _low_battery_alerted = False

    return {"low_battery_alert_mv": alert_mv}


def check_heartbeat_stale() -> int | None:
    """Return hours-since-last-seen if the heartbeat just went stale, else None.

    Returns None when the device has never reported (last_seen == 0), the
    threshold hasn't been crossed, or we already alerted for this episode.
    The flag is cleared the next time update_device_status() runs.
    """
    global _stale_heartbeat_alerted
    last_seen = _device.get("last_seen", 0)
    if last_seen <= 0:
        return None
    delta_s = int(time.time()) - last_seen
    if delta_s > STALE_HEARTBEAT_S and not _stale_heartbeat_alerted:
        _stale_heartbeat_alerted = True
        return delta_s // 3600
    return None


def get_device_status() -> dict:
    """Get last known device status."""
    return dict(_device)
