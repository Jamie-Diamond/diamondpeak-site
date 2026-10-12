"""Morning card wellness line: only numbers off the athlete's own normal (12 Oct 2026)."""
import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "lib"), str(REPO / "ironman-analysis"), str(REPO / "telegram")]
spec = importlib.util.spec_from_file_location("mc", REPO / "scripts" / "morning-checkin.py")
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)

T = date(2026, 10, 12)


def rows(today, hrv=45, rhr=50, sleep_h=7.5):
    out = [{"id": (T - timedelta(days=i)).isoformat(), "hrv": 45, "restingHR": 50,
            "sleepSecs": 7.5 * 3600} for i in range(1, 8)]
    out.append({"id": T.isoformat(), "hrv": hrv, "restingHR": rhr,
                "sleepSecs": None if sleep_h is None else sleep_h * 3600})
    return out


def test_normal_day_shows_nothing():
    assert mc._off_normal_line(rows(T), T) == ""


def test_off_normal_shows_only_what_is_off_with_the_usual():
    assert mc._off_normal_line(rows(T, hrv=38, sleep_h=5.9), T) == \
        "Off your normal: HRV 38 (usual 45) · sleep 5.9h (usual 7.5h)"
    assert mc._off_normal_line(rows(T, rhr=56), T) == "Off your normal: resting HR 56 (usual 50)"


def test_nothing_before_sleep_syncs():
    assert mc._off_normal_line(rows(T, hrv=30, sleep_h=None), T) == ""
