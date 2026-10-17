"""Tests for lib/tri.py, limiters.diagnose_tri and the triathlon card in season_view.
Run: python3 -m pytest ClaudeCoach/lib/test_tri.py"""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import limiters  # noqa: E402
import performance  # noqa: E402
import run_model as rm  # noqa: E402
import season_view as sv  # noqa: E402
import tri  # noqa: E402

FENIX = "Garmin fenix 8"
TODAY = date(2026, 10, 17)
_ids = iter(range(1, 10_000))


def act(when, type_, km, secs, name="x", watts=None, np_w=None):
    """One Intervals.icu activity as the API returns it (only the fields tri.py reads)."""
    a = {"id": f"t{next(_ids)}", "start_date_local": when, "type": type_, "name": name,
         "distance": km * 1000, "moving_time": secs, "elapsed_time": secs,
         "average_heartrate": 140, "athlete_max_hr": 190, "icu_resting_hr": 55,
         "average_speed": km * 1000 / secs if secs else 0, "device_name": FENIX, "race": False}
    if watts:
        a["icu_average_watts"] = watts
        a["icu_weighted_avg_watts"] = np_w or watts
    return a


def race_day(day, swim_km, bike_km, run_km, swim_s=4000, bike_s=17000, run_s=13500,
             watts=180, np_w=190, transitions=True):
    """The activities a multisport race day leaves in Intervals.icu."""
    xs = [act(f"{day}T07:00:00", "OpenWaterSwim", swim_km, swim_s),
          act(f"{day}T08:10:00", "Ride", bike_km, bike_s, watts=watts, np_w=np_w),
          act(f"{day}T13:00:00", "Run", run_km, run_s)]
    if transitions:
        xs += [act(f"{day}T07:50:00", "Transition", 0, 180), act(f"{day}T12:55:00", "Transition", 0, 120)]
    return xs


def race(kind="ironman", day="2025-09-20", swim_s=4000, avg_w=180, np_w=None, bike_s=17000, run_s=13500):
    """A tri.tri_races() entry, built directly (the pure functions take these)."""
    swim_km, bike_km, run_km = tri.DIST[kind]
    return {"date": day, "kind": kind,
            "swim": {"dist_m": swim_km * 1000, "time_s": swim_s},
            "bike": {"id": f"b-{day}", "dist_m": bike_km * 1000, "time_s": bike_s, "avg_w": avg_w, "np_w": np_w},
            "run": {"dist_m": run_km * 1000, "time_s": run_s}}


FLAT = [0.0] * 1801                                   # 180 km, dead flat, 100 m steps


# ── tri_races ────────────────────────────────────────────────────────────────

def test_tri_races_finds_a_day_with_a_transition_and_names_the_kind_from_bike_km():
    acts = (race_day("2025-06-01", 1.5, 35, 10, swim_s=1700, bike_s=4800, run_s=3000)
            + race_day("2025-07-13", 1.9, 90, 21.1, swim_s=2300, bike_s=9500, run_s=6000)
            + race_day("2025-09-20", 3.9, 180.4, 42.5))
    got = tri.tri_races(acts)
    assert [(r["date"], r["kind"]) for r in got] == [("2025-06-01", "olympic"), ("2025-07-13", "70_3"),
                                                     ("2025-09-20", "ironman")]       # oldest first
    assert tri.kind_for_bike_km(20) == "sprint" and tri.kind_for_bike_km(129.9) == "70_3"
    im = got[-1]
    assert im["swim"]["time_s"] == 4000 and im["run"]["time_s"] == 13500
    assert im["bike"]["time_s"] == 17000 and im["bike"]["avg_w"] == 180 and im["bike"]["np_w"] == 190


def test_tri_races_needs_a_transition_and_all_three_sports():
    no_t = race_day("2025-09-20", 3.8, 180, 42.2, transitions=False)       # a brick-style day, not a race
    no_run = [a for a in race_day("2025-10-04", 1.9, 90, 21, ) if a["type"] != "Run"]
    assert tri.tri_races(no_t) == []
    assert tri.tri_races(no_run) == []


def test_tri_races_duplicate_rides_collapse_to_the_one_with_power():
    acts = race_day("2025-09-20", 3.8, 180, 42.2)
    powered_id = next(a["id"] for a in acts if a["type"] == "Ride")
    # a longer copy of the ride (head-unit file) with no power: power still wins
    acts.append(act("2025-09-20T08:10:00", "Ride", 181.5, 17100))
    got = tri.tri_races(acts)
    assert len(got) == 1 and got[0]["bike"]["id"] == powered_id and got[0]["bike"]["avg_w"] == 180


def test_tri_races_without_power_the_longest_ride_wins():
    acts = race_day("2025-09-20", 3.8, 180, 42.2, watts=None)
    acts.append(act("2025-09-20T08:10:00", "Ride", 181.5, 17100, name="copy"))
    got = tri.tri_races(acts)
    assert got[0]["bike"]["dist_m"] == 181_500


def test_tri_races_swim_and_run_are_the_official_distances_not_the_gps():
    acts = race_day("2025-09-20", 4.31, 180, 43.6)                      # wandering swim, long GPS run
    r = tri.tri_races(acts)[0]
    assert r["swim"]["dist_m"] == tri.DIST["ironman"][0] * 1000 == 3800
    assert r["run"]["dist_m"] == tri.DIST["ironman"][2] * 1000 == 42195


# ── swim_factor ──────────────────────────────────────────────────────────────

def test_swim_factor_is_the_median_of_pace_over_css():
    # 3.8 km: 4104 s = 108 s/100 m, 3952 s = 104, 4256 s = 112  ->  1.04 / 1.08 / 1.12 at CSS 100
    races = [race(swim_s=4104), race(swim_s=3952, day="2024-09-20"), race(swim_s=4256, day="2023-09-20")]
    assert tri.swim_factor(races, 100) == (1.08, 3)


def test_swim_factor_ignores_a_race_outside_swim_sane():
    short_course = race(swim_s=3000, day="2024-09-20")                   # 0.79: the swim was short
    long_course = race(swim_s=6000, day="2023-09-20")                    # 1.58: mis-measured
    assert tri.swim_factor([race(swim_s=4104), short_course, long_course], 100) == (1.08, 1)
    assert tri.swim_factor([short_course, long_course], 100) == (tri.SWIM_DEFAULT, 0)


def test_swim_factor_without_css_or_swim_time_is_the_default():
    assert tri.swim_factor([race(swim_s=4104)], None) == (tri.SWIM_DEFAULT, 0)
    assert tri.swim_factor([race(swim_s=4104)], 0) == (tri.SWIM_DEFAULT, 0)
    assert tri.swim_factor([race(swim_s=0)], 100) == (tri.SWIM_DEFAULT, 0)
    assert tri.swim_factor([], 100) == (tri.SWIM_DEFAULT, 0)


# ── variability ──────────────────────────────────────────────────────────────

def test_variability_is_the_median_np_over_avg_of_rides_of_two_hours_plus():
    races = [race(avg_w=200, np_w=210),                                                  # 1.05
             race(avg_w=200, np_w=220, day="2024-09-20"),                                # 1.10
             race(avg_w=200, np_w=240, day="2023-09-20"),                                # 1.20
             race(avg_w=200, np_w=300, bike_s=5400, day="2024-06-01"),                   # 1.5 but 1.5 h: ignored
             race(avg_w=200, np_w=None, day="2024-07-01"),                               # no NP: ignored
             race(avg_w=None, np_w=210, day="2024-08-01")]                               # no avg: ignored
    assert tri.variability(races) == 1.1


def test_variability_with_nothing_usable_is_the_default():
    assert tri.variability([]) == tri.VI_DEFAULT
    assert tri.variability([race(avg_w=200, np_w=300, bike_s=3600)]) == tri.VI_DEFAULT


# ── bike anchors and the power-time law ──────────────────────────────────────

def test_bike_anchors_keep_only_the_given_dates_with_power_and_time():
    races = [race(day="2024-09-20", avg_w=175, bike_s=17400), race(day="2025-09-20", avg_w=180, bike_s=17000),
             race(day="2023-09-20", avg_w=170, bike_s=17800), race(day="2025-06-01", avg_w=None),
             race(day="2025-07-01", avg_w=190, bike_s=0)]
    assert tri.bike_anchors(races, ["2024-09-20", "2025-09-20"]) == [(175, 17400), (180, 17000)]
    assert tri.bike_anchors(races, ["2025-06-01", "2025-07-01"]) == []           # no power / no time
    assert tri.bike_anchors(races, []) == []


def test_bike_time_and_bike_power_for_are_inverses_within_one_percent():
    anchors = [(180, 17000), (190, 16500), (172, 17600)]
    for w in (150, 185, 230):
        t = tri.bike_time(w, anchors)
        assert tri.bike_power_for(t, anchors) == pytest.approx(w, rel=0.01)


def test_more_power_means_less_time():
    anchors = [(180, 17000), (190, 16500)]
    times = [tri.bike_time(w, anchors) for w in (160, 180, 200, 220)]
    assert times == sorted(times, reverse=True)
    powers = [tri.bike_power_for(t, anchors) for t in (19000, 17000, 15000)]
    assert powers == sorted(powers)


def test_one_anchor_time_goes_with_power_to_the_minus_a_third():
    anchors = [(180, 17000)]
    assert tri.bike_time(180, anchors) == pytest.approx(17000)
    assert tri.bike_time(180 * 8, anchors) == pytest.approx(17000 / 2)           # 8x power, half the time
    assert tri.bike_time(180 / 8, anchors) == pytest.approx(17000 * 2)
    assert tri.bike_time(200, anchors) == pytest.approx(17000 * (180 / 200) ** (1 / 3))


def test_bike_time_and_power_for_with_nothing_to_go_on_are_none():
    assert tri.bike_time(180, []) is None and tri.bike_time(0, [(180, 17000)]) is None
    assert tri.bike_power_for(17000, []) is None and tri.bike_power_for(0, [(180, 17000)]) is None


# ── physics fallback ─────────────────────────────────────────────────────────

def test_bike_time_physics_flat_ironman_at_180w_is_five_to_six_hours():
    t = tri.bike_time_physics(180, FLAT, 84, tri.CDA_DEFAULT)
    assert 5 * 3600 < t < 6 * 3600
    assert tri.bike_time_physics(200, FLAT, 84) < t                              # more power, faster
    assert tri.bike_time_physics(180, FLAT, 84, cda=0.30) > t                    # more drag, slower


def test_bike_power_physics_inverts_bike_time_physics_within_2w():
    for w in (150, 180, 230):
        t = tri.bike_time_physics(w, FLAT, 84)
        assert abs(tri.bike_power_physics(t, FLAT, 84) - w) < 2


def test_physics_with_no_profile_or_no_input_is_none():
    assert tri.bike_time_physics(180, [], 84) is None and tri.bike_time_physics(0, FLAT, 84) is None
    assert tri.bike_power_physics(18000, [], 84) is None and tri.bike_power_physics(0, FLAT, 84) is None


# ── off_bike_factor ──────────────────────────────────────────────────────────

def test_off_bike_factor_is_the_median_of_tri_run_over_open_potential():
    seen = []

    def open_s(dist_m, iso):
        seen.append((dist_m, iso))
        return 10000

    races = [race(run_s=10800, day="2025-09-20"),                                  # 1.08
             race(run_s=11000, day="2024-09-20"),                                  # 1.10
             race(run_s=11200, day="2023-09-20"),                                  # 1.12
             race("70_3", run_s=12000, day="2025-07-13")]                          # another kind: ignored
    assert tri.off_bike_factor(races, "ironman", open_s) == (1.1, 3)
    assert all(d == 42195 for d, _ in seen) and ("2025-07-13" not in {i for _, i in seen})


def test_off_bike_factor_ignores_ratios_outside_095_to_16_and_missing_potential():
    races = [race(run_s=11000, day="2025-09-20"),                                  # 1.10 kept
             race(run_s=9000, day="2024-09-20"),                                   # 0.90: a short course
             race(run_s=20000, day="2023-09-20"),                                  # 2.00: a collapse
             race(run_s=0, day="2022-09-20")]                                      # no run time
    assert tri.off_bike_factor(races, "ironman", lambda d, i: 10000) == (1.1, 1)
    assert tri.off_bike_factor(races, "ironman", lambda d, i: None) == (tri.RUN_DEFAULT["ironman"], 0)


def test_off_bike_factor_boundaries_are_inclusive():
    races = [race(run_s=9500, day="2025-09-20"), race(run_s=16000, day="2024-09-20")]
    f, n = tri.off_bike_factor(races, "ironman", lambda d, i: 10000)
    assert n == 2 and f == pytest.approx(1.275)


def test_off_bike_factor_with_no_races_of_the_kind_is_the_default_for_it():
    assert tri.off_bike_factor([], "ironman", lambda d, i: 10000) == (tri.RUN_DEFAULT["ironman"], 0)
    assert tri.off_bike_factor([race("70_3")], "ironman", lambda d, i: 10000) == (1.10, 0)
    assert tri.off_bike_factor([], "70_3", lambda d, i: 10000) == (tri.RUN_DEFAULT["70_3"], 0)
    assert tri.off_bike_factor([], "triple", lambda d, i: 10000) == (1.08, 0)      # unknown kind


# ── goal_splits ──────────────────────────────────────────────────────────────

def test_goal_splits_target_splits_win_when_all_three_are_set():
    target = {"swim_min": 67, "bike_min": 284, "run_min": 210, "t1t2_min": 5.5}
    got = tri.goal_splits(34200, target, race())
    assert got == {"swim": 4020, "bike": 17040, "run": 12600, "t": 330}
    # no transition figure: zero, not the 600 s default (the athlete set the splits)
    assert tri.goal_splits(None, {"swim_min": 67, "bike_min": 284, "run_min": 210}, None)["t"] == 0


def test_goal_splits_share_the_goal_like_the_last_race():
    last = race(swim_s=4000, bike_s=17000, run_s=13000)
    got = tri.goal_splits(34200, None, last)
    assert got["t"] == 600
    assert got["swim"] + got["bike"] + got["run"] + got["t"] == pytest.approx(34200)
    assert got["bike"] / got["swim"] == pytest.approx(17000 / 4000)               # the proportions of the last race
    # partial target splits do not count: the goal time is shared instead
    partial = {"swim_min": 67, "bike_min": 284, "run_min": None}
    assert tri.goal_splits(34200, partial, last) == got
    assert tri.goal_splits(34200, {}, last) == got
    # a recorded transition time is used
    with_t = dict(last, t_s=420)
    g2 = tri.goal_splits(34200, None, with_t)
    assert g2["t"] == 420 and g2["swim"] + g2["bike"] + g2["run"] + g2["t"] == pytest.approx(34200)


def test_goal_splits_with_nothing_to_go_on_is_none():
    assert tri.goal_splits(None, None, None) is None
    assert tri.goal_splits(34200, None, None) is None                              # a goal but no past race
    assert tri.goal_splits(None, None, race()) is None                             # a past race but no goal
    assert tri.goal_splits(None, {"swim_min": 67, "bike_min": 284}, race()) is None


# ── legs() ───────────────────────────────────────────────────────────────────

def tri_block():
    return {"css_s": 100, "swim_factor": [1.05, 2], "vi": 1.1, "off_bike": {"ironman": [1.08, 1]},
            "races": [race("ironman", "2025-09-20", avg_w=180, bike_s=17000)]}


def run_block():
    half = {"date": "2026-03-15", "event": "half_marathon", "dist_m": 21097.5, "time_s": 5600,
            "vdot": rm.vdot(21097.5, 5600)}
    return {"races": [half], "weeklyKm": 40}


BIKE = {"ftp": 300, "weight": 80}


def test_legs_swim_bike_run_from_the_athletes_own_numbers():
    got = tri.legs("ironman", tri_block(), run_block(), BIKE, None, ["2025-09-20"], TODAY)
    assert set(got) == {"swim", "bike", "run"}
    # swim: 3.8 km at 100 s/100 m x 1.05
    assert got["swim"]["time_s"] == pytest.approx(38 * 100 * 1.05) == pytest.approx(3990)
    assert "2 race swims" in got["swim"]["source"]
    # bike: FTP 300 x IF 0.68 (5.6 h) = 204 W NP, / VI 1.1, scaled off the past ride (180 W, 17000 s)
    assert "past ride" in got["bike"]["source"]
    avg = 300 * tri.hold_if(180 / 32.0) / 1.1
    assert got["bike"]["time_s"] == pytest.approx(17000 * (180 / avg) ** (1 / 3))
    assert 16000 < got["bike"]["time_s"] < 17000                                    # a touch more power than 2025
    # run: open marathon potential x the athlete's own off-the-bike factor
    open_now = rm.predict(42195, run_block()["races"], TODAY, 40.0)["now_s"]
    assert got["run"]["time_s"] == pytest.approx(open_now * 1.08)
    assert "past ironman runs" in got["run"]["source"]


def test_legs_bike_hours_changes_the_intensity_held():
    short = tri.legs("ironman", tri_block(), None, BIKE, None, ["2025-09-20"], TODAY, bike_hours=2.0)["bike"]
    assert tri.hold_if(2.0) == 0.80 and "≈" in short["you"]
    base = tri.legs("ironman", tri_block(), None, BIKE, None, ["2025-09-20"], TODAY)["bike"]
    assert short["time_s"] < base["time_s"]                                         # higher IF, more power, faster
    assert tri.hold_if(5.625) == 0.68 and tri.hold_if(99) == 0.62


def test_legs_first_time_on_the_course_uses_physics_and_says_so():
    got = tri.legs("ironman", tri_block(), run_block(), BIKE, FLAT, [], TODAY)
    assert "typical age-group position" in got["bike"]["source"]
    avg = 300 * tri.hold_if(180 / 32.0) / 1.1
    assert got["bike"]["time_s"] == pytest.approx(tri.bike_time_physics(avg, FLAT, 80))


def test_legs_no_anchors_and_no_profile_means_no_bike_leg():
    assert "bike" not in tri.legs("ironman", tri_block(), run_block(), BIKE, None, [], TODAY)
    assert "bike" not in tri.legs("ironman", tri_block(), run_block(), BIKE, [], [], TODAY)
    # a profile but no body weight: physics cannot run either
    assert "bike" not in tri.legs("ironman", tri_block(), run_block(), {"ftp": 300}, FLAT, [], TODAY)


def test_legs_no_ftp_means_no_bike_leg_even_with_anchors():
    got = tri.legs("ironman", tri_block(), run_block(), {"weight": 80}, FLAT, ["2025-09-20"], TODAY)
    assert "bike" not in got and {"swim", "run"} <= set(got)
    assert "bike" not in tri.legs("ironman", tri_block(), run_block(), None, FLAT, ["2025-09-20"], TODAY)


def test_legs_with_nothing_measured_leaves_every_leg_out():
    assert tri.legs("ironman", None, None, None, None, [], TODAY) == {}
    assert tri.legs("ironman", {}, {"races": []}, {}, None, [], TODAY) == {}


def test_legs_default_factors_when_the_athlete_has_no_race_history():
    block = {"css_s": 100, "races": []}                                            # no swim_factor / off_bike keys
    got = tri.legs("ironman", block, run_block(), BIKE, None, [], TODAY)
    assert got["swim"]["time_s"] == pytest.approx(38 * 100 * tri.SWIM_DEFAULT)
    assert "no race swims" in got["swim"]["source"]
    open_now = rm.predict(42195, run_block()["races"], TODAY, 40.0)["now_s"]
    assert got["run"]["time_s"] == pytest.approx(open_now * tri.RUN_DEFAULT["ironman"])
    assert "typical off-the-bike slowing" in got["run"]["source"]


def test_legs_use_the_distances_of_the_kind():
    block = {"css_s": 100, "swim_factor": [1.0, 1]}
    assert tri.legs("70_3", block, None, None, None, [], TODAY)["swim"]["time_s"] == pytest.approx(19 * 100)
    assert tri.legs("olympic", block, None, None, None, [], TODAY)["swim"]["time_s"] == pytest.approx(15 * 100)


# ── diagnose_tri ─────────────────────────────────────────────────────────────

SPLITS = {"swim": 4020, "bike": 17040, "run": 12600, "t": 330}                     # 1:07, 4:44, 3:30


def leg(time_s, you="you", source="src"):
    return {"time_s": time_s, "you": you, "source": source}


def on_target(**over):
    out = {"swim": leg(SPLITS["swim"]), "bike": leg(SPLITS["bike"]), "run": leg(SPLITS["run"])}
    out.update(over)
    return out


def rows_by_key(d):
    return {r["key"]: r for r in d["rows"]}


def test_diagnose_tri_without_splits_asks_for_a_goal():
    d = limiters.diagnose_tri("ironman", None, on_target(), None)
    assert "Set a goal time" in d["headline"] and d["limiter"] is None
    by = rows_by_key(d)
    assert [by[k]["need"] for k in ("swim", "bike", "run")] == ["no goal split"] * 3
    assert all(by[k]["status"] == "unknown" and by[k]["short"] is None for k in ("swim", "bike", "run"))
    assert by["swim"]["you"] == "you"                                              # the prediction is still shown


def test_diagnose_tri_names_the_run_when_it_is_twelve_minutes_over():
    legs = on_target(bike=leg(SPLITS["bike"] + 60), run=leg(SPLITS["run"] + 720))
    d = limiters.diagnose_tri("ironman", SPLITS, legs, None, goal="sub 9:30")
    assert d["limiter"] == "run"
    assert d["headline"].startswith("Run off the bike is the issue")
    assert "(+12 min)" in d["headline"] and "3:30" in d["headline"] and "sub 9:30" in d["headline"]
    by = rows_by_key(d)
    assert by["bike"]["status"] == "green" and by["bike"]["minutes"] == 1          # 1 min of 4:44 is 0.4%
    assert by["bike"]["need"] == "4:44" and by["swim"]["need"] == "1:07" and by["run"]["need"] == "3:30"
    assert by["swim"]["status"] == "green" and by["run"]["status"] == "amber"      # 5.7% over
    assert d["fix"] and "Runs off long rides" in d["fix"]


def test_diagnose_tri_seven_percent_over_is_red_and_four_percent_amber():
    legs = on_target(swim=leg(SPLITS["swim"] * 1.07), bike=leg(SPLITS["bike"] * 1.04))
    by = rows_by_key(limiters.diagnose_tri("ironman", SPLITS, legs, None))
    assert by["swim"]["status"] == "red" and by["bike"]["status"] == "amber" and by["run"]["status"] == "green"
    assert by["swim"]["short"] == pytest.approx(0.07, abs=1e-3) and by["bike"]["short"] == pytest.approx(0.04, abs=1e-3)


def test_diagnose_tri_limiter_is_the_leg_costing_most_minutes_not_the_biggest_percentage():
    legs = on_target(swim=leg(SPLITS["swim"] * 1.07), bike=leg(SPLITS["bike"] * 1.04))     # swim +4.7 min, bike +11.4 min
    d = limiters.diagnose_tri("ironman", SPLITS, legs, None)
    assert d["limiter"] == "bike" and d["headline"].startswith("Bike is the issue")


def test_diagnose_tri_a_leg_faster_than_its_split_is_green_with_no_shortfall():
    legs = on_target(run=leg(SPLITS["run"] - 600))
    by = rows_by_key(limiters.diagnose_tri("ironman", SPLITS, legs, None))
    assert by["run"]["status"] == "green" and by["run"]["short"] == 0.0


def test_diagnose_tri_durability_drift_is_the_limiter_when_the_legs_are_on_target():
    d = limiters.diagnose_tri("ironman", SPLITS, on_target(), None, durability={"drift_pct": 12},
                              fuel={"g_per_h": 80, "n": 3})
    assert d["limiter"] == "durability"
    assert d["headline"].startswith("Durability is the issue") and "12%" in d["headline"]
    by = rows_by_key(d)
    assert by["durability"]["status"] == "red" and by["fuelling"]["status"] == "green"
    assert by["durability"]["need"] == f"under {limiters.DRIFT_OK:.0f}%"


def test_diagnose_tri_drift_bands():
    def status(drift):
        return rows_by_key(limiters.diagnose_tri("ironman", SPLITS, on_target(), None,
                                                 durability={"drift_pct": drift}))["durability"]["status"]
    assert status(limiters.DRIFT_OK) == "green" and status(8) == "amber" and status(10.5) == "red"


def test_diagnose_tri_fuelling_below_the_range_is_flagged_and_inside_it_is_green():
    low = limiters.diagnose_tri("ironman", SPLITS, on_target(), None, fuel={"g_per_h": 30})
    assert low["limiter"] == "fuelling" and rows_by_key(low)["fuelling"]["status"] == "red"
    ok = limiters.diagnose_tri("ironman", SPLITS, on_target(), None, fuel={"g_per_h": limiters.FUEL_G_H[0]})
    assert rows_by_key(ok)["fuelling"]["status"] == "green"
    assert f"{limiters.FUEL_G_H[0]}–{limiters.FUEL_G_H[1]} g an hour" == rows_by_key(ok)["fuelling"]["need"]


def test_diagnose_tri_nothing_measured_says_so():
    d = limiters.diagnose_tri("ironman", SPLITS, {}, None, durability=None, fuel=None)
    assert "Not enough measured yet" in d["headline"] and d["limiter"] is None
    assert {r["status"] for r in d["rows"]} == {"unknown"}
    assert [r["key"] for r in d["rows"]] == ["fitness", "swim", "bike", "run", "durability", "fuelling"]
    assert rows_by_key(d)["bike"]["need"] == "4:44"                                # the split is still shown


def test_diagnose_tri_everything_fine_says_nothing_is_holding_you_back():
    fit = {"now": 95.0, "lo": 90, "hi": 105}
    d = limiters.diagnose_tri("ironman", SPLITS, on_target(), fit, durability={"drift_pct": 2},
                              fuel={"g_per_h": 75}, goal="sub 9:30")
    assert d["limiter"] is None and d["headline"].startswith("Nothing is holding you back")
    assert rows_by_key(d)["fitness"]["status"] == "green"


def test_diagnose_tri_fitness_row_judges_against_the_share_due_by_this_phase():
    fit = {"now": 50.0, "lo": 90, "hi": 105}
    d = limiters.diagnose_tri("ironman", SPLITS, on_target(), fit, phase_frac=0.6)
    row = rows_by_key(d)["fitness"]
    assert "~54 by now" in row["need"] and "90–105 race day" in row["need"]
    assert row["short"] == pytest.approx(0.074, abs=1e-3)                          # 50 against 54 due
    assert row["status"] == "amber"                                                # past GREEN (3%), within RED (10%)
    full = rows_by_key(limiters.diagnose_tri("ironman", SPLITS, on_target(), fit))["fitness"]
    assert full["need"] == "90–105" and full["status"] == "red"                    # no phase: against the whole band


def test_diagnose_tri_red_durability_outranks_amber_fuelling():
    d = limiters.diagnose_tri("ironman", SPLITS, on_target(), None, durability={"drift_pct": 15},
                              fuel={"g_per_h": 55})
    by = rows_by_key(d)
    assert by["durability"]["status"] == "red" and by["fuelling"]["status"] == "amber"
    assert d["limiter"] == "durability"


# ── season_view: how a tri A race reaches the card ───────────────────────────

IM_RACE = {"name": "IM Italy Emilia-Romagna", "date": "2027-09-18", "priority": "A",
           "distance": "Full Ironman", "goal": "sub 9:30"}
CFG = {"race_name": "IM Italy Emilia-Romagna",
       "race_target_splits": {"swim_min": 67, "bike_min": 284, "run_min": 210, "t1t2_min": 5.5}}


def _season_data():
    return {"performance": {"tri": tri_block(), "run": run_block(),
                            "bike": {"ftp": 300, "weight": 80, "fuel": {"g_per_h": 80, "n": 3}}}}


def _patch(monkeypatch, races):
    import planning_pause
    import races as races_lib
    monkeypatch.setattr(planning_pause, "is_paused", lambda slug, cfg=None: False)
    monkeypatch.setattr(races_lib, "load_races", lambda slug, config=None: races)


def test_payload_tri_a_race_gets_six_limiter_rows_with_the_goal_splits(monkeypatch):
    _patch(monkeypatch, [dict(IM_RACE)])
    p = sv.payload("x", dict(CFG), {}, _season_data(), today=TODAY)
    assert p["aRace"]["kind"] == "tri" and p["aRace"]["event"] == "ironman"
    lim = p["limiters"]
    assert [r["key"] for r in lim["rows"]] == ["fitness", "swim", "bike", "run", "durability", "fuelling"]
    by = rows_by_key(lim)
    assert by["bike"]["need"] == "4:44" and by["swim"]["need"] == "1:07" and by["run"]["need"] == "3:30"
    assert by["swim"]["you"].startswith("CSS 1:40") and by["run"]["you"].startswith("open ")
    assert by["fuelling"]["status"] == "green" and by["fitness"]["status"] == "unknown"      # no CTL in the data
    assert lim["headline"] and lim["foot"]


def test_payload_tri_without_target_splits_shares_the_goal_like_the_last_race(monkeypatch):
    _patch(monkeypatch, [dict(IM_RACE)])
    p = sv.payload("x", {"race_name": "Some other race"}, {}, _season_data(), today=TODAY)
    by = rows_by_key(p["limiters"])
    scale = (34200 - 600) / (4000 + 17000 + 13500)                                  # the 2025 race, goal sub 9:30
    assert by["swim"]["need"] == limiters._hms(4000 * scale)
    assert by["bike"]["need"] == limiters._hms(17000 * scale)
    assert by["run"]["need"] == limiters._hms(13500 * scale)


def test_payload_tri_target_splits_belong_to_the_current_race_only(monkeypatch):
    other = dict(IM_RACE, name="Another Ironman")
    _patch(monkeypatch, [other])
    p = sv.payload("x", dict(CFG), {}, _season_data(), today=TODAY)                 # CFG is for the Italy race
    assert rows_by_key(p["limiters"])["bike"]["need"] != "4:44"


def test_payload_tri_with_no_performance_data_still_shows_the_split_rows(monkeypatch):
    _patch(monkeypatch, [dict(IM_RACE)])
    p = sv.payload("x", dict(CFG), {}, {}, today=TODAY)
    by = rows_by_key(p["limiters"])
    assert by["bike"]["need"] == "4:44" and by["bike"]["status"] == "unknown"
    assert "Not enough measured yet" in p["limiters"]["headline"]


# ── performance._css_s / tri_block (the data tri.py is fed) ──────────────────

def test_css_s_reads_the_profile_then_the_intervals_swim_threshold():
    assert performance._css_s({"swim_css_per_100m": "1:41"}) == 101
    assert performance._css_s({"swim_css_per_100m": "1:41.5"}) == 101.5
    assert performance._css_s({}) is None and performance._css_s(None) is None

    class Client:
        def get_sport_settings(self, sport):
            assert sport == "Swim"
            return [{"threshold_pace": 1.0}]                                        # 1 m/s = 100 s per 100 m
    assert performance._css_s({}, Client()) == 100.0
    assert performance._css_s({"swim_css_per_100m": "1:41"}, Client()) == 101      # profile wins


def test_tri_block_from_activities():
    acts = race_day("2025-09-20", 3.9, 180.4, 42.5, swim_s=4104, bike_s=17000, run_s=13500, watts=180, np_w=198)
    block = performance.tri_block(acts, {"races": []}, {"swim_css_per_100m": "1:40"})
    assert block["css_s"] == 100 and block["swim_factor"] == [1.08, 1] and block["vi"] == 1.1
    assert [r["kind"] for r in block["races"]] == ["ironman"]
    assert block["off_bike"] == {"ironman": [tri.RUN_DEFAULT["ironman"], 0]}       # no open-run potential yet
    assert performance.tri_block([], {"races": []}, {}) is None                     # no race, no CSS
