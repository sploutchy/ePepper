import device_telemetry
from device_telemetry import LOW_BATTERY_MV, LOW_BATTERY_REARM_MV, update_device_status


def setup_function(_):
    device_telemetry._low_battery_alerted = False


def test_alert_fires_once_below_threshold():
    assert update_device_status(LOW_BATTERY_MV - 50, -60)["low_battery_alert_mv"] == LOW_BATTERY_MV - 50
    # Repeat reports while still low stay quiet.
    assert update_device_status(LOW_BATTERY_MV - 100, -60)["low_battery_alert_mv"] is None


def test_noise_inside_hysteresis_band_does_not_rearm():
    update_device_status(LOW_BATTERY_MV - 10, -60)  # fires + latches
    # Bounces just above LOW but below REARM must not re-arm…
    update_device_status(LOW_BATTERY_MV + 10, -60)
    assert update_device_status(LOW_BATTERY_MV - 10, -60)["low_battery_alert_mv"] is None


def test_recovery_above_rearm_re_enables_alert():
    update_device_status(LOW_BATTERY_MV - 10, -60)
    update_device_status(LOW_BATTERY_REARM_MV, -60)  # charged past the band
    assert update_device_status(LOW_BATTERY_MV - 10, -60)["low_battery_alert_mv"] is not None


def test_zero_reading_is_ignored():
    # battery_mv=0 means "no reading" — never alerts, never re-arms.
    assert update_device_status(0, -60)["low_battery_alert_mv"] is None


def test_firmware_version_not_blanked_by_old_report():
    update_device_status(4000, -60, firmware_version=42)
    update_device_status(4000, -60)  # pre-OTA build omits the field
    assert device_telemetry.get_device_status()["firmware_version"] == 42
