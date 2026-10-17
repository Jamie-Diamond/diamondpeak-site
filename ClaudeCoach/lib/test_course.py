"""Tests for lib/course.py, limiters.diagnose_bike and the bike branch of season_view. Run:
python3 -m pytest ClaudeCoach/lib/test_course.py"""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import course as cs  # noqa: E402
import limiters  # noqa: E402
import race_fitness as rf  # noqa: E402
import season_view as sv  # noqa: E402

DEG_M = 111.195        # one 0.001 degree step along a meridian, in metres (R = 6371 km)


# ── builders ──────────────────────────────────────────────────────────────────

def meridian(n_points, ele_at, lat0=51.0, lon=-1.0):
    """[(lat, lon, ele)] due north in 0.001 degree steps (~111 m apart)."""
    return [(round(lat0 + i * 0.001, 6), lon, ele_at(i)) for i in range(n_points)]


def gpx_text(points, tag="trkpt", lon_first=False):
    rows = []
    for lat, lon, ele in points:
        attrs = f'lon="{lon}" lat="{lat}"' if lon_first else f'lat="{lat}" lon="{lon}"'
        rows.append(f"  <{tag} {attrs}>\n    <ele>{ele}</ele>\n  </{tag}>")
    return '<?xml version="1.0"?>\n<gpx>\n<trk><trkseg>\n' + "\n".join(rows) + "\n</trkseg></trk>\n</gpx>"


def ramp_then_flat(flat_before_km, climb_km, pct, flat_after_km):
    """An elevation list at 100 m steps: flat, a steady climb, flat at the top."""
    n_up = int(round(climb_km * 10))
    top = n_up * pct             # pct % of 100 m = pct metres a step
    return ([0.0] * (int(round(flat_before_km * 10)) + 1)
            + [pct * i for i in range(1, n_up + 1)]
            + [float(top)] * int(round(flat_after_km * 10)))


def synthetic_course(**over):
    """60 km: 20 km flat, a 5 km climb at 6% (300 m), then flat. Start 06:30, a cut-off at
    km 40 (08:30), a finish at km 60 (11:00), feeds at km 15 and km 50."""
    c = {"name": "Synth 60", "match": ["synth 60"], "start": "06:30",
         "ele": ramp_then_flat(20, 5, 6, 35),
         "checkpoints": [{"km": 40, "clock": "08:30", "type": "cutoff"},
                         {"km": 30, "clock": "07:30", "type": "info"},
                         {"km": 60, "clock": "11:00", "type": "finish", "label": "Finish"}],
         "feeds": [{"km": 15, "label": "A"}, {"km": 50, "label": "B"}]}
    c.update(over)
    return c


MALLORCA = cs.load(cs.RACES_DIR / "mallorca-312.json")
HEAVY_BIKE = {"ftp": 211, "weight": 93, "outdoor": {"rides": 10, "with_power": 0}, "longest_h": 8.7}
STRONG_BIKE = {"ftp": 330, "weight": 70, "outdoor": {"rides": 10, "with_power": 10},
               "durability": {"drift_pct": 3, "n": 4}, "fuel": {"g_per_h": 75, "n": 4}, "longest_h": 11}
FITNESS = {"now": 26, "lo": 28, "hi": 40}


def row(diag, key):
    return next(r for r in diag["rows"] if r["key"] == key)


# ── parse_gpx ─────────────────────────────────────────────────────────────────

def test_parse_gpx_trkpt_lat_then_lon():
    pts = cs.parse_gpx(gpx_text([(51.5, -0.12, 10.5), (51.6, -0.13, -3.0)]))
    assert pts == [(51.5, -0.12, 10.5), (51.6, -0.13, -3.0)]


def test_parse_gpx_lon_before_lat_is_returned_as_lat_lon_ele():
    pts = cs.parse_gpx(gpx_text([(39.7, 2.9, 120.0), (39.8, 2.95, 130.0)], lon_first=True))
    assert pts == [(39.7, 2.9, 120.0), (39.8, 2.95, 130.0)]


def test_parse_gpx_reads_rtept():
    pts = cs.parse_gpx(gpx_text([(51.0, -1.0, 5.0), (51.1, -1.1, 6.0)], tag="rtept"))
    assert pts == [(51.0, -1.0, 5.0), (51.1, -1.1, 6.0)]


def test_parse_gpx_ignores_points_without_elevation():
    text = ('<gpx><trkseg>'
            '<trkpt lat="51.0" lon="-1.0"><ele>10</ele></trkpt>'
            '<trkpt lat="51.1" lon="-1.0"></trkpt>'
            '<trkpt lat="51.2" lon="-1.0"/>'
            '<trkpt lat="51.3" lon="-1.0"><time>2026-01-01T00:00:00Z</time></trkpt>'
            '<trkpt lat="51.4" lon="-1.0" ><ele>40</ele></trkpt>'
            '</trkseg></gpx>')
    assert cs.parse_gpx(text) == [(51.0, -1.0, 10.0), (51.4, -1.0, 40.0)]
    assert cs.parse_gpx("") == []
    assert cs.parse_gpx("<gpx></gpx>") == []


# ── profile / ascent ──────────────────────────────────────────────────────────

def test_profile_is_one_value_per_100_m_of_track():
    pts = meridian(101, lambda i: 100.0)                 # 100 steps = ~11.12 km, flat
    ele = cs.profile(pts)
    length_m = 100 * DEG_M
    assert len(ele) == int(length_m) // 100 + 1 == 112
    assert (len(ele) - 1) * 100 <= length_m < len(ele) * 100
    assert set(ele) == {100.0}
    assert cs.profile(meridian(101, lambda i: 100.0), step_m=500) == [100.0] * (int(length_m) // 500 + 1)


def test_profile_of_a_ramp_rises_at_the_track_gradient_and_ascent_matches():
    pts = meridian(101, lambda i: 5.0 * i)               # +5 m per ~111 m = 4.5%
    ele = cs.profile(pts)
    per_100m = 5.0 / DEG_M * 100
    assert all(abs((ele[i + 1] - ele[i]) - per_100m) < 0.15 for i in range(1, len(ele) - 2))
    assert abs(ele[50] - 50 * 100 / DEG_M * 5.0) < 0.5
    assert 485 < cs.ascent(ele) < 505                    # ~500 m climbed (end smoothing trims a little)


def test_profile_needs_two_points():
    assert cs.profile([]) == []
    assert cs.profile([(51.0, -1.0, 10.0)]) == []


def test_ascent_counts_only_the_ups():
    assert cs.ascent([0, 5, 10, 15, 10, 20]) == 25
    assert cs.ascent([100, 90, 80]) == 0
    assert cs.ascent([5]) == 0


# ── climbs ────────────────────────────────────────────────────────────────────

def test_climbs_finds_a_3km_6pct_climb_and_names_it_from_a_nearby_summit():
    ele = ramp_then_flat(2, 3, 6, 2)                    # flat 2 km, 3 km at 6%, flat 2 km
    found = cs.climbs(ele, [{"name": "Coll Test", "km": 6.0}, {"name": "Far Away", "km": 40.0}])
    assert len(found) == 1
    c = found[0]
    assert c["length_km"] == pytest.approx(3.0, abs=0.3)
    assert c["avg_pct"] == pytest.approx(6.0, abs=0.5)
    assert c["gain_m"] == pytest.approx(180, abs=6)
    assert c["top_km"] == pytest.approx(5.0, abs=0.2) and c["top_m"] == 180
    assert c["name"] == "Coll Test"                      # summit 1 km past the top: within 3 km


def test_climbs_leaves_the_name_empty_when_no_summit_is_within_3_km():
    ele = ramp_then_flat(2, 3, 6, 2)
    assert cs.climbs(ele, [{"name": "Too Far", "km": 9.0}])[0]["name"] is None
    assert cs.climbs(ele)[0]["name"] is None


def test_climbs_picks_the_nearest_summit():
    ele = ramp_then_flat(2, 3, 6, 5)
    c = cs.climbs(ele, [{"name": "Near", "km": 5.5}, {"name": "Further", "km": 7.0}])[0]
    assert c["name"] == "Near"


def test_a_50m_bump_is_not_a_climb():
    ele = ramp_then_flat(2, 1, 5, 2)                     # 1 km at 5% = 50 m
    assert max(ele) - min(ele) == 50 < cs.CLIMB_MIN_GAIN
    assert cs.climbs(ele) == []


def test_climbs_ignores_flat_and_descents():
    assert cs.climbs([100.0] * 60) == []
    assert cs.climbs([300.0 - 6 * i for i in range(50)]) == []


# ── speed ─────────────────────────────────────────────────────────────────────

def test_speed_on_the_flat_at_200w_85kg():
    kmh = cs.speed(200, 0.0, 85) * 3.6
    assert 30 < kmh < 40


def test_speed_up_8pct_at_200w_85kg():
    kmh = cs.speed(200, 0.08, 85) * 3.6
    assert 8 < kmh < 13


def test_speed_balances_the_power_equation():
    import math
    for power, grade, mass in ((200, 0.0, 85), (200, 0.08, 85), (300, 0.04, 96), (150, -0.01, 80)):
        v = cs.speed(power, grade, mass)
        a = math.atan(grade)
        demand = (mass * cs.G * (math.sin(a) + cs.CRR * math.cos(a)) * v
                  + 0.5 * cs.RHO * cs.CDA * v ** 3)
        assert demand == pytest.approx(power * cs.DRIVETRAIN, rel=1e-6)


def test_speed_rises_with_power_and_falls_with_weight_and_gradient():
    assert cs.speed(250, 0.06, 85) > cs.speed(200, 0.06, 85)
    assert cs.speed(200, 0.06, 105) < cs.speed(200, 0.06, 85)
    assert cs.speed(200, 0.10, 85) < cs.speed(200, 0.06, 85) < cs.speed(200, 0.0, 85)


def test_speed_never_exceeds_vmax_downhill():
    assert cs.speed(0, -0.08, 85) == cs.VMAX
    assert cs.speed(400, -0.10, 85) == cs.VMAX
    for grade in (-0.15, -0.08, -0.03, -0.01, 0.0, 0.02):
        for power in (0, 150, 400, 1000):
            assert cs.speed(power, grade, 85) <= cs.VMAX + 1e-9
    assert cs.VMAX == pytest.approx(50 / 3.6)


# ── climbing_split and needs ──────────────────────────────────────────────────

def test_climbing_split_counts_climbing_and_the_rest():
    c = synthetic_course()
    secs, climb_km, rest_km = cs.climbing_split(c["ele"], 0, 40, 200, 96)
    assert climb_km == pytest.approx(5.0) and rest_km == pytest.approx(35.0)
    assert secs == pytest.approx(5000 / cs.speed(200, 0.06, 96), rel=1e-6)    # 50 steps of 100 m at 6%
    # a window with no climbing in it
    assert cs.climbing_split(c["ele"], 30, 40, 200, 96) == (0.0, 0.0, pytest.approx(10.0))


def test_needs_budget_is_the_clock_difference_and_stops_count_feeds_before_each_checkpoint():
    n = cs.needs(synthetic_course(), 200, 85)
    assert [x["label"] for x in n] == ["km 40", "Finish"]            # the "info" checkpoint is skipped
    cut, fin = n
    assert cut["budget_min"] == 120 and fin["budget_min"] == 270     # 06:30 -> 08:30 / 11:00
    assert cut["stops_min"] == cs.STOP_MIN_PER_FEED * 1               # the km 15 feed
    assert fin["stops_min"] == cs.STOP_MIN_PER_FEED * 2               # km 15 and km 50
    assert cut["clock"] == "08:30" and fin["km"] == 60


def test_needs_rest_speed_is_the_distance_left_over_the_time_left():
    c = synthetic_course()
    cut = cs.needs(c, 200, 85)[0]
    secs, climb_km, rest_km = cs.climbing_split(c["ele"], 0, 40, 200, 85 + cs.KIT_KG)
    assert cut["climb_min"] == round(secs / 60)
    assert cut["climb_km"] == 5.0 and cut["rest_km"] == 35.0
    expected = rest_km / ((120 - cut["stops_min"] - secs / 60) / 60)
    assert cut["rest_kmh"] == pytest.approx(expected, abs=0.05)


def test_needs_more_power_means_less_climbing_time_and_a_lower_rest_speed():
    c = synthetic_course()
    low, high = cs.needs(c, 150, 85), cs.needs(c, 250, 85)
    for lo, hi in zip(low, high):
        assert hi["climb_min"] < lo["climb_min"]
        assert hi["rest_kmh"] < lo["rest_kmh"]
        assert hi["stops_min"] == lo["stops_min"] and hi["budget_min"] == lo["budget_min"]


def test_needs_a_heavier_rider_needs_more_rest_speed():
    c = synthetic_course()
    assert cs.needs(c, 200, 100)[0]["rest_kmh"] > cs.needs(c, 200, 70)[0]["rest_kmh"]


def test_needs_is_none_when_the_climbing_alone_blows_the_cutoff():
    c = synthetic_course(checkpoints=[{"km": 40, "clock": "06:40", "type": "cutoff"}])
    n = cs.needs(c, 200, 85)
    assert n[0]["budget_min"] == 10 and n[0]["rest_kmh"] is None


def test_needs_without_feeds_or_checkpoints():
    assert cs.needs(synthetic_course(feeds=None), 200, 85)[0]["stops_min"] == 0
    assert cs.needs(synthetic_course(checkpoints=[]), 200, 85) == []


# ── climb_power_for ───────────────────────────────────────────────────────────

def test_climb_power_for_meets_the_target_at_every_cutoff_and_not_much_below():
    c = synthetic_course()
    p = cs.climb_power_for(c, 85, 25)
    assert p is not None
    assert max(n["rest_kmh"] for n in cs.needs(c, p, 85)) <= 25.05
    assert max(n["rest_kmh"] for n in cs.needs(c, p - 5, 85)) > 25.0   # it is the least power that does


def test_climb_power_for_a_faster_rest_speed_needs_less_climbing_power():
    c = synthetic_course()
    p25, p30, p40 = (cs.climb_power_for(c, 85, t) for t in (25, 30, 40))
    assert p25 > p30 > p40


def test_climb_power_for_is_none_when_no_power_can_make_it():
    # even with no time on the climbs, 35 km in 116 min is ~18 km/h: a 15 km/h target can't bind
    assert cs.climb_power_for(synthetic_course(), 85, 15) is None


def test_climb_power_for_on_mallorca_is_a_plausible_power():
    p30, p35 = cs.climb_power_for(MALLORCA, 93, 30), cs.climb_power_for(MALLORCA, 93, 35)
    assert 150 < p35 < p30 < 300
    assert max(n["rest_kmh"] for n in cs.needs(MALLORCA, p30, 93)) <= 30.05


# ── load / for_race / from_gpx ────────────────────────────────────────────────

def test_load_returns_none_for_a_missing_or_broken_file(tmp_path):
    assert cs.load(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert cs.load(bad) is None


@pytest.fixture
def course_dirs(tmp_path, monkeypatch):
    races = tmp_path / "reference" / "races"
    mine = tmp_path / "athletes" / "jane" / "courses"
    races.mkdir(parents=True)
    mine.mkdir(parents=True)
    monkeypatch.setattr(cs, "BASE", tmp_path)
    monkeypatch.setattr(cs, "RACES_DIR", races)
    return races, mine


def put(d, fname, **c):
    (d / fname).write_text(json.dumps(c))


def test_for_race_prefers_the_athletes_own_upload(course_dirs):
    races, mine = course_dirs
    put(races, "test-sportive.json", name="Test Sportive", match=["test sportive"], tag="reference")
    put(mine, "my-test.json", name="Test Sportive (mine)", match=["test sportive"], tag="athlete")
    assert cs.for_race("Test Sportive 2027", "jane")["tag"] == "athlete"
    assert cs.for_race("Test Sportive 2027")["tag"] == "reference"           # no slug: reference only
    assert cs.for_race("Test Sportive 2027", "someone-else")["tag"] == "reference"


def test_for_race_matches_loosely_on_the_normalised_name(course_dirs):
    races, _ = course_dirs
    put(races, "a.json", name="Test Sportive", match=["test sportive"], tag="ref")
    assert cs.for_race("TEST-sportive!")["tag"] == "ref"
    assert cs.for_race("The Test Sportive, Mallorca")["tag"] == "ref"


def test_for_race_unknown_name_is_none(course_dirs):
    races, _ = course_dirs
    put(races, "a.json", name="Test Sportive", match=["test sportive"])
    (races / "junk.json").write_text("not json")                              # skipped, not fatal
    assert cs.for_race("Brighton Marathon") is None
    assert cs.for_race("Brighton Marathon", "jane") is None
    assert cs.for_race("") is None and cs.for_race(None) is None


def test_for_race_without_match_falls_back_to_the_lowercase_name(course_dirs):
    races, _ = course_dirs
    put(races, "a.json", name="Ride London", tag="ref")
    assert cs.for_race("Ride London 100")["tag"] == "ref"


def test_for_race_with_no_course_directories_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "BASE", tmp_path)
    monkeypatch.setattr(cs, "RACES_DIR", tmp_path / "missing")
    assert cs.for_race("Anything", "jane") is None


def test_for_race_name_fallback_survives_punctuation(course_dirs):
    races, _ = course_dirs
    put(races, "a.json", name="Ironman 70.3 Emilia", tag="ref")
    assert cs.for_race("Ironman 70.3 Emilia") is not None


def test_for_race_nameless_course_does_not_match_everything(course_dirs):
    races, _ = course_dirs
    put(races, "a.json", start="07:00", ele=[0, 1, 2])
    assert cs.for_race("Brighton Marathon") is None


def test_from_gpx_builds_a_course_dict():
    pts = meridian(101, lambda i: 5.0 * i)
    c = cs.from_gpx(gpx_text(pts), "My Ride (2027)!", start="07:00")
    assert c["name"] == "My Ride (2027)!" and c["match"] == ["my ride 2027"]
    assert c["start"] == "07:00" and c["step_m"] == cs.STEP_M
    assert c["distance_km"] == pytest.approx(11.1, abs=0.1) and 485 < c["ascent_m"] < 505
    assert c["distance_km"] == round((len(c["ele"]) - 1) * 0.1, 1)
    assert c["climbs"] and c["climbs"][0]["gain_m"] > 400


def test_a_course_with_no_cutoffs_has_nothing_timed_so_no_climbing_power_is_asked_for():
    c = cs.from_gpx(gpx_text(meridian(101, lambda i: 5.0 * i)), "My Ride", start="07:00")
    assert c.get("checkpoints") is None and cs.needs(c, 200, 80) == []
    assert cs.climb_power_for(c, 80, 30) is None
    d = limiters.diagnose_bike(c, {"ftp": 250, "weight": 80, "outdoor": {"rides": 3, "with_power": 3}},
                               FITNESS, 0.7)
    assert row(d, "climbing")["status"] == "unknown"


# ── the real Mallorca 312 file ────────────────────────────────────────────────

def test_mallorca_file_shape():
    m = MALLORCA
    assert m["name"] == "Mallorca 312" and "mallorca 312" in m["match"]
    assert 305 < m["distance_km"] < 320
    assert abs((len(m["ele"]) - 1) * cs.STEP_M / 1000 - m["distance_km"]) < 0.2
    assert m["official_ascent_m"] == 5050 and m["year"]
    assert any(c["km"] == 97 and c["clock"] == "11:20" for c in m["checkpoints"])
    assert m["start"] == "06:30" and [c["type"] for c in m["checkpoints"]].count("finish") == 1
    assert m["checkpoints"][-1]["type"] == "finish" and m["checkpoints"][-1]["km"] == 312


def test_mallorca_stored_climbs_and_ascent_match_the_code():
    m = MALLORCA
    assert cs.ascent(m["ele"]) == m["ascent_m"]
    assert cs.climbs(m["ele"], m["summits"]) == m["climbs"]
    assert "Puig Major" in [c["name"] for c in m["climbs"]]


def test_mallorca_is_found_by_for_race():
    assert cs.for_race("Mallorca 312")["name"] == "Mallorca 312"
    assert cs.for_race("Road — Mallorca312 2027")["name"] == "Mallorca 312"


def test_mallorca_at_160w_and_93kg_the_climbs_leave_over_30_kmh_to_find_by_km_97():
    first = cs.needs(MALLORCA, 160, 93)[0]
    assert first["km"] == 97 and first["clock"] == "11:20" and first["budget_min"] == 290
    assert first["climb_min"] > first["budget_min"] / 2          # the climbs alone take most of the 4h50
    assert first["stops_min"] == cs.STOP_MIN_PER_FEED            # the km 50 feed
    assert first["rest_kmh"] > 30


# ── limiters.diagnose_bike ────────────────────────────────────────────────────

def test_bike_without_a_course_asks_for_the_route():
    d = limiters.diagnose_bike(None, HEAVY_BIKE, FITNESS, 0.7)
    assert d["headline"].startswith("Send the route")
    assert row(d, "climbing")["need"] == "send the route (GPX)"
    assert row(d, "climbing")["status"] == "unknown"
    assert d["limiter"] is None and d["fix"] is None


def test_bike_without_a_course_still_judges_fitness():
    behind = {"now": 12, "lo": 28, "hi": 40}
    d = limiters.diagnose_bike(None, HEAVY_BIKE, behind, 0.7)
    assert row(d, "fitness")["status"] == "red" and d["limiter"] == "fitness"
    assert d["headline"].startswith("Send the route")


def test_bike_with_nothing_to_go_on_is_none():
    assert limiters.diagnose_bike(MALLORCA, None, None) is None


def test_heavy_rider_on_mallorca_is_held_back_by_climbing_and_needs_a_power_meter():
    d = limiters.diagnose_bike(MALLORCA, HEAVY_BIKE, FITNESS, 0.7)
    assert d["limiter"] == "climbing"
    assert d["headline"].startswith("Climbing power is the issue")
    assert "km 97" in d["headline"] and "11:20" in d["headline"]
    climbing = row(d, "climbing")
    assert climbing["status"] == "red" and climbing["short"] > limiters.RED
    assert climbing["source"] == "indoor power" and climbing["you"].startswith("2.3 W/kg FTP (211 W at 93 kg)")
    assert "W/kg on the climbs" in climbing["need"] and "W/kg (" in d["headline"]
    assert row(d, "durability")["you"].startswith("needs a power meter")
    assert row(d, "durability")["status"] == "unknown" and row(d, "durability")["why"] == "power"
    assert row(d, "fuelling")["status"] == "unknown"
    assert "each 10 W" in d["fix"] and "each 3 kg" in d["fix"]
    assert "power meter" in d["foot"]
    assert [r["key"] for r in d["rows"]] == ["fitness", "climbing", "durability", "fuelling"]
    assert "8.7 h longest" in row(d, "durability")["you"]          # context, not a pass mark


def test_heavy_rider_fix_savings_are_positive_minutes():
    import re
    d = limiters.diagnose_bike(MALLORCA, HEAVY_BIKE, FITNESS, 0.7)
    w10, kg3 = (int(x) for x in re.findall(r"~(\d+) min", d["fix"]))
    assert w10 > 0 and kg3 > 0


def test_a_short_longest_ride_is_never_the_verdict():
    """Jamie, 17 Oct 2026: nobody rides 14 h to prepare for a 14 h ride."""
    d = limiters.diagnose_bike(MALLORCA, dict(STRONG_BIKE, longest_h=5), {"now": 30, "lo": 28, "hi": 40}, 0.7)
    assert d["limiter"] is None and d["headline"].startswith("Nothing is holding you back")
    assert "longest_ride_h" not in d["targets"]


def test_strong_rider_with_a_long_enough_longest_ride_has_nothing_holding_them_back():
    for longest in (11, 12):
        d = limiters.diagnose_bike(MALLORCA, dict(STRONG_BIKE, longest_h=longest),
                                   {"now": 30, "lo": 28, "hi": 40}, 0.7)
        assert row(d, "climbing")["status"] == "green"
        assert d["limiter"] is None
        assert d["headline"].startswith("Nothing is holding you back")
        assert d["fix"] is None


def test_low_fuelling_with_everything_else_green_is_the_limiter():
    d = limiters.diagnose_bike(MALLORCA, dict(STRONG_BIKE, longest_h=12, fuel={"g_per_h": 40, "n": 4}),
                               {"now": 30, "lo": 28, "hi": 40}, 0.7)
    assert d["limiter"] == "fuelling"
    assert d["headline"].startswith("Fuelling is the issue")
    assert row(d, "fuelling")["status"] == "red" and row(d, "fuelling")["short"] == pytest.approx(0.333, abs=0.001)
    assert "60-90 g" in d["fix"]


def test_the_biggest_gap_wins_when_several_are_flagged():
    d = limiters.diagnose_bike(MALLORCA, dict(HEAVY_BIKE, fuel={"g_per_h": 40, "n": 3}), FITNESS, 0.7)
    assert row(d, "climbing")["short"] < row(d, "fuelling")["short"]
    assert d["limiter"] == "fuelling"


def test_bike_row_short_is_never_negative():
    d = limiters.diagnose_bike(MALLORCA, STRONG_BIKE, {"now": 60, "lo": 28, "hi": 40}, 0.7)
    assert all(r["short"] is None or r["short"] >= 0 for r in d["rows"])


# ── season_view integration ───────────────────────────────────────────────────

def test_levels_key_treats_a_road_ride_with_a_distance_as_a_sportive():
    assert rf.levels_key("Road — 312 km") == "sportive"
    assert rf.levels_key("Road - 100 km") == "sportive"
    assert rf.event_kind("sportive") == "bike"


def test_payload_for_a_bike_a_race_carries_the_course_and_the_bike_card(monkeypatch):
    import planning_pause
    import races
    monkeypatch.setattr(planning_pause, "is_paused", lambda slug, cfg=None: False)
    monkeypatch.setattr(races, "load_races", lambda slug, config=None: [
        {"name": "Mallorca 312", "date": "2027-04-24", "priority": "A", "distance": "Road — 312 km"}])
    cfg = {"plan_start": "2026-11-02", "race_date": "2027-04-24",
           "phase_tss": {"base_end_week": 8, "build_end_week": 18}}
    data = {"kpi": {"ctl": 26}, "performance": {"bike": HEAVY_BIKE}, "resolvedFtp": 211}
    p = sv.payload("nobody-test", cfg, {}, data, today=date(2026, 10, 17))
    a = p["aRace"]
    assert a["kind"] == "bike" and a["event"] == "sportive"
    assert a["course"]["name"] == "Mallorca 312"
    assert a["course"]["distance_km"] == MALLORCA["distance_km"] and a["course"]["ascent_m"] == 5050
    fin = next(c for c in MALLORCA["checkpoints"] if c["type"] == "finish")
    day_h = (cs._clock_min(fin["clock"]) - cs._clock_min(MALLORCA["start"])) / 60
    assert a["hold"]["hours"] == pytest.approx(day_h, abs=0.05)         # the day the finish cut-off allows
    rows = {r["key"]: r for r in p["limiters"]["rows"]}
    assert rows["climbing"]["status"] == "red"
    # No goal: fitness is judged against a 10 h+ day (50-65), never his current 26 (Jamie,
    # 17 Oct 2026: "if you can do the 312 on a fitness of 40 I'll be amazed").
    assert p["fitness"]["lo"] >= 50 and p["fitness"]["source"] == "day length"
    assert rows["fitness"]["status"] == "red" and p["limiters"]["limiter"] in ("fitness", "climbing")
    assert p["limiters"]["headline"].startswith(("Climbing power is the issue", "Fitness is the issue"))


def test_a_route_with_nothing_measured_never_reads_as_all_clear():
    bare = {"name": "My route", "ele": [0.0] * 50}
    d = limiters.diagnose_bike(bare, {"outdoor": {"rides": 0, "with_power": 0}}, None)
    assert "Nothing is holding you back" not in d["headline"]
    assert d["headline"].startswith("Send the start time and cut-offs")



def test_a_fitness_target_never_comes_from_current_fitness():
    """Circularity guard: with no goal, the band is the event's (or the day's), whatever
    fitness the athlete has now - so it can't sit next to where they already are."""
    import season_view as sv
    for now in (10, 26, 45, 80):
        fb = sv.fitness_band("sportive", None, {}, {}, {"kpi": {"ctl": now}}, hours=12)
        assert (fb["lo"], fb["hi"]) == (50, 65) and fb["source"] == "day length"
        short = sv.fitness_band("sportive", None, {}, {}, {"kpi": {"ctl": now}})
        assert (short["lo"], short["hi"]) == (28, 40) and short["source"] == "finish level"


def test_no_plan_phases_judges_fitness_by_weeks_to_race():
    import season_view as sv
    from datetime import date
    today = date(2026, 10, 17)
    assert sv._phase_frac(None, today, "2027-04-24") == 0.70        # 27 weeks out: base
    assert sv._phase_frac(None, today, "2026-11-20") == 0.90        # ~5 weeks out: specific
