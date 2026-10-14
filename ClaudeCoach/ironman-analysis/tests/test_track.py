"""lib/track.py: GPS track + zone colours for Peak's map (Jamie, 14 Oct 2026)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import track  # noqa: E402

SS = {"ftp": 200, "power_zones": [55, 75, 90, 105, 120, 150, 999], "hr_zones": [120, 140, 150, 160, 170, 180, 200],
      "threshold_pace": 4.0, "pace_zones": [78, 88, 94, 100, 104, 112, 999]}


def streams(n=200, watts=150, hr=145, vel=3.6):
    return [{"type": "latlng", "data": [51.5 + i * 1e-4 for i in range(n)], "data2": [-0.1] * n},
            {"type": "watts", "data": [watts] * n}, {"type": "heartrate", "data": [hr] * n},
            {"type": "velocity_smooth", "data": [vel] * n}]


def test_ride_has_power_and_hr_not_pace():
    t = track.build(streams(), SS, "Ride")
    assert len(t["pts"]) == 200 and set(t["ch"]) == {"power", "hr"}
    assert set(t["ch"]["power"]) == {2}                   # 75% FTP


def test_run_has_pace_and_hr_never_power():
    t = track.build(streams(), SS, "Run")
    assert set(t["ch"]) == {"pace", "hr"}


def test_no_gps_is_none_and_long_tracks_are_sampled():
    assert track.build([{"type": "heartrate", "data": [140] * 50}], SS, "Run") is None
    assert len(track.build(streams(n=5000), SS, "Ride")["pts"]) <= track.MAX_PTS


def test_find_activity_by_day_sport_and_name():
    acts = [{"id": 1, "start_date_local": "2026-10-13T07:00:00", "type": "Ride", "name": "A"},
            {"id": 2, "start_date_local": "2026-10-13T18:00:00", "type": "Ride", "name": "B"},
            {"id": 3, "start_date_local": "2026-10-13T08:00:00", "type": "Run", "name": "C"}]
    assert track.find_activity(acts, "2026-10-13", "Bike", "B")["id"] == 2
    assert track.find_activity(acts, "2026-10-13", "Run", None)["id"] == 3
    assert track.find_activity(acts, "2026-10-14", "Ride", None) is None
