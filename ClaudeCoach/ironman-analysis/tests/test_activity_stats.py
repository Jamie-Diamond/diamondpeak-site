"""lib/activity_stats.py: the session sheet's stats (Jamie, 14 Oct 2026)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import activity_stats as A  # noqa: E402


def test_peak_is_best_rolling_mean():
    assert A.peak([100] * 10 + [400] * 5 + [100] * 10, 5) == 400
    assert A.peak([100] * 3, 5) is None


def test_decoupling_cuts_warmup_and_coasting():
    n = 3600
    watts = [200] * n
    hr = [130] * 1800 + [143] * 1800                  # HR up 10% in the 2nd half
    hr[:600] = [100] * 600                             # warm-up HR, must not count
    for i in range(1000, 1300):
        watts[i] = 0                                   # coasting, must not count
    d = A.decoupling(watts, hr, {}, True)
    assert 6.5 < d < 7.1                               # 1 - 133.25/143: warm-up and coasting left out


def test_too_short_or_intervals_gives_no_number():
    assert A.decoupling([200] * 1500, [130] * 1500, {}, True) is None
    act = {"icu_weighted_avg_watts": 180, "icu_average_watts": 150}
    rows = dict(A.build(act, [{"type": "watts", "data": [200] * 4000},
                              {"type": "heartrate", "data": [140] * 4000}], "Ride"))
    assert rows["Decoupling"] == "not for interval sessions"
    assert rows["Peak power"].startswith("5 s 200")


def test_run_gets_pace_never_ef_or_power():
    act = {"distance": 10000, "moving_time": 3000, "gap": 3.4, "icu_efficiency_factor": 3.0,
           "icu_weighted_avg_watts": 400, "average_cadence": 85}
    rows = dict(A.build(act, [], "Run"))
    assert rows["Avg pace"] == "5:00/km" and rows["Cadence"] == "170 spm"
    assert "Efficiency (EF)" not in rows and "Normalised power" not in rows
