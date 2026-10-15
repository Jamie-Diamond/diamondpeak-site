"""Run laps: at least 3, none over 5 km, split evenly (Jamie, 15 Oct 2026)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from primitives.planned_tss import render_workout, run_laps, tss_from_segments  # noqa: E402

FAST = 1000 / 242          # 4:02/km threshold


def mins(segs):
    return [s["minutes"] for s in segs]


def test_one_step_run_becomes_warm_up_main_warm_down():
    out = run_laps([{"minutes": 30, "zone": "z2"}])
    assert mins(out) == [5, 20, 5]


def test_long_main_splits_evenly_under_5km():
    out = run_laps([{"minutes": 60, "zone": "z2"}], FAST)
    assert len(out) == 4 and abs(out[1]["minutes"] - out[2]["minutes"]) <= 1


def test_warm_up_plus_long_main_gets_a_warm_down():
    out = run_laps([{"minutes": 10, "zone": "z1"}, {"minutes": 50, "zone": "z2"}], FAST)
    assert out[0] == {"minutes": 10, "zone": "z1"} and out[-1]["minutes"] == 5 and len(out) >= 3


def test_intervals_left_alone():
    segs = [{"minutes": 15, "zone": "z2"},
            {"repeat": 4, "steps": [{"minutes": 4, "zone": "threshold"}, {"minutes": 2, "zone": "z1"}]},
            {"minutes": 10, "zone": "z2"}]
    assert run_laps(segs, FAST) == segs


def test_load_unchanged_and_bikes_untouched():
    r = render_workout("Run", [{"minutes": 60, "zone": "z2"}], threshold_mps=FAST)
    assert r["tss"] == tss_from_segments("Run", [{"minutes": 60, "zone": "z2"}])["tss"]
    assert render_workout("Ride", [{"minutes": 60, "zone": "z2"}])["steps"] == 1
