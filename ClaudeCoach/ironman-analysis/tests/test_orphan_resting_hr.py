"""A resting HR with no HRV and no sleep that day is an all-day reading, not an
overnight one: left out for athletes whose watch normally sends HRV (Jamie, 15 Oct 2026)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
from icu_api import drop_orphan_resting_hr  # noqa: E402


def _row(d, rhr, hrv=None, sleep=None):
    return {"id": d, "restingHR": rhr, "hrv": hrv, "sleepSecs": sleep}


def test_a_dropout_day_is_left_out_for_an_hrv_athlete():
    rows = [_row("2026-07-29", 55, 33.0, 23880), _row("2026-07-30", 53, 40.0, 28246),
            _row("2026-07-31", 67), _row("2026-08-01", 60, sleep=25000)]
    out = drop_orphan_resting_hr(rows)
    assert "restingHR" not in out[2] and out[2]["restingHR_raw"] == 67
    assert out[0]["restingHR"] == 55 and out[3]["restingHR"] == 60   # sleep alone keeps it


def test_an_athlete_without_hrv_keeps_every_value():
    rows = [_row("2026-07-30", 53), _row("2026-07-31", 67), _row("2026-08-01", 60, 40.0, 1)]
    assert [r.get("restingHR") for r in drop_orphan_resting_hr(rows)] == [53, 67, 60]


def test_empty_and_missing_values_are_fine():
    assert drop_orphan_resting_hr([]) == []
    rows = [_row("2026-08-10", None)]
    assert drop_orphan_resting_hr(rows)[0]["restingHR"] is None
