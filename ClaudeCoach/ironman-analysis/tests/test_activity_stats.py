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
    act = {"icu_weighted_avg_watts": 240, "icu_average_watts": 150}
    rows = dict(A.build(act, [{"type": "watts", "data": [200] * 4000},
                              {"type": "heartrate", "data": [140] * 4000}], "Ride"))
    assert rows["Decoupling"] == "n/a: intervals or surges (power too variable)"
    coasty = dict(A.build({"icu_weighted_avg_watts": 205, "icu_average_watts": 140},
                          [{"type": "watts", "data": ([200] * 7 + [0] * 3) * 400},
                           {"type": "heartrate", "data": [140] * 4000}], "Ride"))
    assert coasty["Decoupling"].endswith("%")              # coasting doesn't block it
    assert rows["Peak power"].startswith("5 s 200")


def test_run_gets_pace_never_ef_or_power():
    act = {"distance": 10000, "moving_time": 3000, "gap": 3.4, "icu_efficiency_factor": 3.0,
           "icu_weighted_avg_watts": 400, "average_cadence": 85}
    rows = dict(A.build(act, [], "Run"))
    assert rows["Avg pace"] == "5:00/km" and rows["Cadence"] == "170 spm"
    assert "Efficiency (EF)" not in rows and "Normalised power" not in rows


def _act(i, typ="Run", hr=140, gap=3.3, ifv=None, ef=None, secs=3000):
    return {"id": i, "type": typ, "moving_time": secs, "average_heartrate": hr, "gap": gap,
            "icu_hr_zones": [133, 146, 159], "icu_intensity": ifv, "icu_efficiency_factor": ef}


def test_efficiency_vs_normal_runs_and_rides():
    hist = [_act(i, gap=3.3) for i in range(5)]
    assert A.efficiency_vs_normal(_act(9, gap=3.4), hist, False) == "+3% vs your last 5 easy runs"
    assert A.efficiency_vs_normal(_act(9, hr=155), hist, False) is None        # not easy
    rides = [_act(i, "Ride", ifv=65, ef=1.20) for i in range(4)]
    assert A.efficiency_vs_normal(_act(9, "Ride", ifv=68, ef=1.26), rides, True).startswith("+5%")
    assert A.efficiency_vs_normal(_act(9, "Ride", ifv=85, ef=1.26), rides, True) is None


def test_peak_star_against_the_90_day_best():
    rows = dict(A.build({}, [{"type": "watts", "data": [300] * 400}], "Ride", [], {5: 300, 60: 350}))
    assert rows["Peak power"].startswith("5 s 300★ · 1 min 300 ·")


def test_run_split_and_ride_fade():
    n = 2400
    st = {"distance": [i * 3.0 for i in range(n)], "velocity_smooth": [3.0] * 1200 + [3.3] * 1200}
    assert A.split(st, {}, False).startswith("negative split")
    assert A.split({"watts": [200] * 3600 + [150] * 3600}, {}, True) == "last hour -25% power vs the first"


def test_hr_recovery_needs_an_easing():
    hr = [150] * 300 + [180] * 30 + [150] * 300
    eased = [250] * 330 + [100] * 300
    pushing = [250] * 630
    assert A.hr_recovery(hr, eased).startswith("-30 bpm")
    assert A.hr_recovery(hr, pushing) is None


def test_zone_bars():
    act = {"icu_zone_times": [{"id": "Z1", "secs": 10}, {"id": "Z2", "secs": 20}, {"id": "SS", "secs": 5}],
           "icu_hr_zone_times": [5, 5]}
    assert A.zones(act, "Ride") == [{"label": "Power", "secs": [10, 20]}, {"label": "Heart rate", "secs": [5, 5]}]
