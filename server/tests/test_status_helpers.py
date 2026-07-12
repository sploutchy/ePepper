import time

import status_helpers
from status_helpers import battery_label, battery_pct, humanize_date, rssi_quality, source_name


def test_battery_pct_endpoints_and_midpoint():
    assert battery_pct(4200) == 100
    assert battery_pct(5000) == 100
    assert battery_pct(3300) == 0
    assert battery_pct(3000) == 0
    assert battery_pct(3700) == 50


def test_battery_label_buckets():
    assert battery_label(5) == "critical"
    assert battery_label(29) == "low"
    assert battery_label(59) == "fair"
    assert battery_label(84) == "good"
    assert battery_label(100) == "full"


def test_rssi_quality_buckets():
    assert rssi_quality(-40) == "excellent"
    assert rssi_quality(-55) == "good"
    assert rssi_quality(-65) == "fair"
    assert rssi_quality(-75) == "weak"
    assert rssi_quality(-90) == "poor"


def test_humanize_date(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(status_helpers.time, "time", lambda: now)
    assert humanize_date(None) == "—"
    assert humanize_date(now - 30) == "just now"
    assert humanize_date(now - 5 * 60) == "5 min ago"
    assert humanize_date(now - 3 * 3600) == "3 h ago"
    assert humanize_date(now - 86400) == "yesterday"
    assert humanize_date(now - 4 * 86400) == "4 days ago"
    assert humanize_date(now - 10 * 86400) == "last week"
    assert humanize_date(now - 20 * 86400) == "2 weeks ago"
    assert humanize_date(now - 45 * 86400) == "last month"
    assert humanize_date(now - 200 * 86400) == "6 months ago"
    assert humanize_date(now - 400 * 86400) == "last year"
    assert humanize_date(now - 800 * 86400) == "2 years ago"
    # Clock skew (future timestamp) folds to a safe value, not a crash.
    assert humanize_date(now + 500) == "just now"


def test_source_name():
    assert source_name("https://www.fooby.ch/fr/recettes/1/x") == "Fooby"
    assert source_name("https://bbcgoodfood.com/r") == "Bbcgoodfood"
    assert source_name("cookbook://nos-recettes/crepes") == "Nos-recettes"
    assert source_name("cookbook://a1b2c3") is None
    assert source_name("jsonld:abc") is None
    assert source_name(None) is None
