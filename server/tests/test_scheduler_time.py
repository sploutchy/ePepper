"""The DST behaviour documented in scheduler.py's docstrings, verified.

Europe/Zurich: spring-forward 2026-03-29 (02:00 → 03:00),
fall-back 2026-10-25 (03:00 → 02:00).
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from scheduler import _seconds_until_next_local_midnight, seconds_until_next_local_hour

TZ = ZoneInfo("Europe/Zurich")


def test_midnight_plain_day():
    now = datetime(2026, 7, 10, 18, 0, tzinfo=TZ)
    assert _seconds_until_next_local_midnight(now) == 6 * 3600


def test_midnight_across_spring_forward_is_23h():
    # 2026-03-29 02:00 CET jumps to 03:00 CEST — the day is 23 h long.
    now = datetime(2026, 3, 29, 0, 0, tzinfo=TZ)
    assert _seconds_until_next_local_midnight(now) == 23 * 3600


def test_midnight_across_fall_back_is_25h():
    now = datetime(2026, 10, 25, 0, 0, tzinfo=TZ)
    assert _seconds_until_next_local_midnight(now) == 25 * 3600


def test_next_hour_same_day():
    now = datetime(2026, 7, 10, 4, 0, tzinfo=TZ)
    assert seconds_until_next_local_hour(now, 6) == 2 * 3600


def test_next_hour_rolls_to_tomorrow():
    now = datetime(2026, 7, 10, 7, 0, tzinfo=TZ)
    assert seconds_until_next_local_hour(now, 6) == 23 * 3600


def test_sub_minute_result_rolls_a_full_day_forward():
    # A button press seconds before the target hour must NOT return <60 s
    # (the firmware would treat it as clock skew and drift) — it returns
    # the following day's occurrence instead.
    now = datetime(2026, 7, 10, 5, 59, 30, tzinfo=TZ)
    s = seconds_until_next_local_hour(now, 6)
    assert s == 24 * 3600 + 30


def test_next_hour_across_spring_forward():
    # 23:00 the night before the jump; 06:00 next day is only 6 real hours
    # away because 02:00-03:00 doesn't exist.
    now = datetime(2026, 3, 28, 23, 0, tzinfo=TZ)
    assert seconds_until_next_local_hour(now, 6) == 6 * 3600


def test_next_hour_across_fall_back():
    # 23:00 the night before fall-back; 06:00 next day is 8 real hours away.
    now = datetime(2026, 10, 24, 23, 0, tzinfo=TZ)
    assert seconds_until_next_local_hour(now, 6) == 8 * 3600
