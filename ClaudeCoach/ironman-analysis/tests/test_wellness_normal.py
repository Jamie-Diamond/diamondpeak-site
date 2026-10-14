"""lib/wellness_normal.py: SD-based normal for HRV and resting HR (Jamie, 14 Oct 2026)."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import wellness_normal as WN  # noqa: E402

T = date(2026, 10, 14)
NOISE = [0, 3, -2, 4, -4, 1, -1, 2, -3, 0]


def rows(last7_hrv=None, today_hrv=None, today_rhr=None):
    out = []
    for i in range(35, 0, -1):
        d = T - timedelta(days=i)
        out.append({"id": d.isoformat(), "hrv": 60 + NOISE[i % 10], "restingHR": 50 + NOISE[i % 10] / 2})
    if last7_hrv:
        for r in out[-6:]:
            r["hrv"] = last7_hrv
    out.append({"id": T.isoformat(), "hrv": today_hrv or 60, "restingHR": today_rhr or 50})
    return out


def test_normal_day_is_not_off():
    a = WN.assess(rows(), "hrv", T)
    assert not a["trend_off"] and not a["day_off"] and 58 < a["usual"] < 62
    assert not WN.off_any(rows(), T)


def test_one_low_morning_is_a_day_flag_not_a_trend():
    a = WN.assess(rows(today_hrv=56), "hrv", T)
    assert a["day_off"] and not a["trend_off"]


def test_a_low_week_is_a_trend():
    a = WN.assess(rows(last7_hrv=56, today_hrv=57), "hrv", T)
    assert a["trend_off"] and not a["day_off"]


def test_high_hrv_or_low_resting_hr_is_never_off():
    assert not WN.off_any(rows(today_hrv=75, today_rhr=44), T)


def test_resting_hr_high_morning():
    a = WN.assess(rows(today_rhr=55), "restingHR", T)
    assert a["day_off"]


def test_too_little_baseline_says_nothing():
    assert WN.assess(rows()[-10:], "hrv", T) is None
