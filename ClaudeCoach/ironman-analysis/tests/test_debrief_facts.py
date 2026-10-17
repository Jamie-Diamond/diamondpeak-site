"""The activity debrief's computed facts (Jamie, 16 Oct 2026: shadow review).

A Haiku-vs-Sonnet shadow week found the debrief prompt leaving decisions and arithmetic to
the model: a stale CSS from a field the prompt never fetched, PLAN_DELTA "none" for a ride
Intervals.icu had paired with its plan, zone sums off by 7-16 points, decoupling invented
from laps, flat pace for an outdoor run, a 13-second recording asked "How did it feel?",
422 W called "easy" at 93% of run threshold, and advice about tomorrow. Each is now
computed in scripts/activity-watcher.py and handed over as a finished line (or held by the
code after the model answers). These tests pin each one.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
sys.path.insert(0, str(REPO / "ironman-analysis"))


def _watcher():
    spec = importlib.util.spec_from_file_location("activity_watcher",
                                                  REPO / "scripts" / "activity-watcher.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


aw = _watcher()

RIDE = {"id": "i197659367", "type": "GravelRide", "name": "Endurance ride", "moving_time": 3580,
        "icu_training_load": 39, "paired_event_id": 143545085, "icu_average_watts": 158,
        "decoupling": -5.402064, "icu_ftp": 297,
        "icu_zone_times": [{"id": "Z1", "secs": 1417}, {"id": "Z2", "secs": 1282},
                           {"id": "Z3", "secs": 594}, {"id": "Z4", "secs": 197},
                           {"id": "Z5", "secs": 58}, {"id": "Z6", "secs": 32},
                           {"id": "Z7", "secs": 0}, {"id": "SS", "secs": 305}],
        "icu_hr_zone_times": [1997, 1345, 226, 12, 0]}
RUN = {"id": "i197074440", "type": "Run", "name": "Hinckley", "moving_time": 2435,
       "icu_training_load": 60, "icu_average_watts": 422, "icu_ftp": 452,
       "icu_power_zones": [55, 75, 90, 105, 120, 150, 999], "gap": 3.4364, "average_speed": 3.3,
       "decoupling": -1.98, "icu_hr_zone_times": [283, 1618, 2773, 0, 0]}
KATHRYN_RUN = {"id": "i197461272", "type": "Run", "name": "Wandsworth Running", "moving_time": 2200,
               "icu_training_load": 37, "gap": 2.7754207, "average_speed": 2.741,
               "decoupling": None, "icu_hr_zone_times": [578, 1473, 151, 0, 0, 0, 0]}


def _prompt(**kw):
    return aw._build_prompt("testathlete", "Jamie", 297, [], {"run_threshold_pace_per_km": "4:02"},
                            run_hr_cap=150, **kw)


# 1. CSS ---------------------------------------------------------------------------------

def test_css_written_on_athlete_line_from_icu():
    p = _prompt(css="1:41")
    assert "Jamie: FTP 297 W. Swim CSS 1:41/100m" in p
    assert "profile swim_css_per_100m" not in p


def test_no_css_says_so_rather_than_inviting_a_guess():
    assert "No swim CSS is set" in _prompt(css=None)


def test_resolve_css_reads_icu_swim_threshold(monkeypatch, tmp_path):
    cfg = tmp_path / "athletes.json"
    cfg.write_text(json.dumps({"x": {"icu_athlete_id": "1", "icu_api_key": "k"}}))
    monkeypatch.setattr(aw, "ATHLETES_CONFIG", cfg)

    class Fake:
        def __init__(self, *a):
            pass

        def get_sport_settings(self, sport=None):
            assert sport == "Swim"
            return {"threshold_pace": 100 / 101}       # 1:41 per 100m, in m/s
    monkeypatch.setitem(sys.modules, "icu_api", types.SimpleNamespace(IcuClient=Fake))
    assert aw._resolve_css("x") == "1:41"


# 2. PLAN_DELTA --------------------------------------------------------------------------

def test_plan_delta_from_paired_event():
    ev = {"id": 143545085, "name": "Endurance ride", "type": "Ride", "icu_training_load": 39}
    assert aw._plan_delta(RIDE, ev) == "Endurance ride|39|39|0"
    assert aw._plan_delta(RIDE, None) is None


def test_plan_delta_percent_from_the_whole_numbers_shown():
    ev = {"name": "Easy run", "type": "Run", "icu_training_load": 85.6}
    a = {"icu_training_load": 107.6}
    assert aw._plan_delta(a, ev) == "Easy run|86|108|26"


def test_computed_plan_delta_beats_model_none():
    meta = {"plan_delta": "Endurance ride|39|39|0"}
    assert aw._hold_plan_delta("none", meta, "Solid Z2.", "", "i1") == "Endurance ride|39|39|0"


def test_unpaired_activity_forces_none():
    assert aw._hold_plan_delta("Some plan|50|40|-20", {"plan_delta": None}, "", "", "i1") == "none"


def test_structure_not_executed_still_gives_none():
    meta = {"plan_delta": "8x100|40|35|-12"}
    struct = "Swim (i1) vs 8x100: planned efforts ... | planned structure NOT fully executed (1 of 3 efforts found)"
    assert aw._hold_plan_delta("8x100|40|35|-12", meta, "", struct, "i1") == "none"
    assert aw._hold_plan_delta("none", meta, "Planned structure not executed: two blocks.", "", "i1") == "none"


def test_without_facts_model_line_stands():
    assert aw._hold_plan_delta("X|1|2|100", None, "", "", "i1") == "X|1|2|100"


def test_prompt_no_longer_asks_model_to_match_events():
    p = _prompt()
    assert "delta_pct = round(" not in p
    assert "copy the PLAN_DELTA line from this activity's COMPUTED FACTS" in p


# 3. Zone split --------------------------------------------------------------------------

def test_ride_with_power_uses_power_zones_without_sweetspot():
    line = aw._zone_split_line(RIDE)
    assert line.startswith("Zone split (power zones")
    # from raw secs: Z1-Z2 2699/3580 = 75%, Z3+ 881/3580 = 25%, Z4+ 287/3580 = 8%
    assert "Z1-Z2 75%" in line and "Z3 and above 25%" in line and "Z4 and above 8%" in line
    assert "SS" not in line


def test_run_uses_hr_zones_and_untrusted_hr_gives_nothing():
    line = aw._zone_split_line(RUN)
    assert line.startswith("Zone split (HR zones") and "Z1-Z2 41%" in line
    assert aw._zone_split_line(RUN, hr_trusted=False) == ""


def test_prompt_points_zone_rules_at_computed_line():
    p = _prompt()
    assert "never add zone percentages up yourself" in p
    assert "icu_zone_times or extended_metrics" not in p


# 4. Decoupling --------------------------------------------------------------------------

def test_decoupling_is_icu_field_only():
    assert aw._decoupling_line(RIDE) == "Decoupling (Intervals.icu): -5.4%"
    assert "not available" in aw._decoupling_line(KATHRYN_RUN)
    assert "not available" in aw._decoupling_line(RIDE, hr_trusted=False)


def test_prompt_never_asks_model_to_compute_decoupling_from_laps():
    p = _prompt()
    assert "exclude laps that fall within icu_warmup_time" not in p
    assert "Never work out decoupling or HR drift yourself" in p


# 5. GAP ---------------------------------------------------------------------------------

def test_outdoor_run_without_strava_gets_gap_not_flat_pace():
    lines = aw._gap_lines(KATHRYN_RUN, threshold_s=304)
    assert lines[0].startswith("Avg GAP 6:00/km") and "56 s/km slower than threshold 5:04/km" in lines[0]
    assert "6:05" not in " ".join(lines)          # the flat pace


def test_work_intervals_carry_gap():
    ivs = [{"type": "WORK", "moving_time": 300, "gap": 1000 / 290},
           {"type": "RECOVERY", "moving_time": 60, "gap": 2.0}]
    lines = aw._gap_lines(KATHRYN_RUN, ivs)
    assert lines[1].endswith("1) 5:00 at 4:50/km")


def test_treadmill_flat_pace_and_missing_gap_gives_no_pace():
    assert "flat pace" in aw._gap_lines({**KATHRYN_RUN, "type": "VirtualRun"})[0]
    assert "give no pace figure" in aw._gap_lines({**KATHRYN_RUN, "gap": None})[0]
    assert aw._gap_lines(RIDE) == []


def test_prompt_gap_rule_covers_athletes_without_strava():
    assert "Never quote flat pace for an outdoor run, with or without Strava" in _prompt()


# 6. Scope -------------------------------------------------------------------------------

def test_scope_bans_tomorrow_advice_and_caps_next_time():
    p = _prompt()
    assert "No advice about tomorrow" in p
    assert 'At most ONE "Next time:" line' in p
    assert "run HR cap 150 bpm" in p and "threshold pace 4:02/km" in p


# 7. Tiny recordings ---------------------------------------------------------------------

def test_tiny_recording_is_flagged_with_no_analysis():
    lines, pd, tiny = aw._activity_facts({"type": "Run", "moving_time": 13, "icu_training_load": 0})
    assert tiny and pd is None and lines[0].startswith("TINY RECORDING")
    msg = aw._tiny_message({"type": "Run", "moving_time": 13})
    assert "13 seconds" in msg and "accidental start" in msg
    assert "feel" not in msg.lower()


def test_two_minutes_is_not_tiny():
    assert aw._activity_facts({**KATHRYN_RUN, "moving_time": 120})[2] is False


# 8. Run power ---------------------------------------------------------------------------

def test_run_power_as_percent_of_run_threshold():
    line = aw._run_power_line(RUN)
    assert line == "Running power: avg 422 W = 93% of run threshold power 452 W (power zone Z4)"
    assert aw._run_power_line(RIDE) == ""
    assert aw._run_power_line(KATHRYN_RUN) == ""


def test_prompt_run_power_never_easy_at_z3_plus():
    p = _prompt()
    assert "Never call a run easy when its power is Z3 or above" in p
    assert "pace-power check" not in p


# The whole facts block for one activity -------------------------------------------------

def test_activity_facts_ride_block():
    ev = {"name": "Endurance ride", "type": "Ride", "icu_training_load": 39}
    lines, pd, tiny = aw._activity_facts(RIDE, ev)
    assert not tiny and pd == "Endurance ride|39|39|0"
    assert lines[-1] == "PLAN_DELTA: Endurance ride|39|39|0"
    assert any(l.startswith("Decoupling (Intervals.icu)") for l in lines)


# 17 Oct shadow review (Jamie, 17 Oct 2026: shadow review) ------------------------------
# James's 16 Oct swim: 1.7 km in 1753 s moving. Sonnet said 1:37/100m (the stub formula was
# distance / (time / 100) = 97 m per 100 s); the real pace is 1:43. Haiku wrote a line per
# block, and PLAN_DELTA split the two models because no code checked the swim structure.

JM_SWIM = {"id": "i198039192", "type": "Swim", "name": "Pool Swim", "distance": 1700.0,
           "moving_time": 1753, "icu_training_load": 13, "paired_event_id": 143809460,
           "icu_hr_zone_times": [1700, 18, 18, 17]}
JM_INTERVALS = [{"type": "WORK", "distance": 500.0, "moving_time": 519},
                {"type": "RECOVERY", "distance": None, "moving_time": 61},
                {"type": "WORK", "distance": 400.0, "moving_time": 419},
                {"type": "RECOVERY", "distance": None, "moving_time": 67},
                {"type": "WORK", "distance": 300.0, "moving_time": 314},
                {"type": "WORK", "distance": 200.0, "moving_time": 211},
                {"type": "WORK", "distance": 100.0, "moving_time": 101},
                {"type": "WORK", "distance": 200.0, "moving_time": 206}]
_DRILLS = [{"duration": 60, "pace": {"start": 62, "end": 72, "units": "%pace"}},
           {"duration": 30, "pace": {"start": 58, "end": 66, "units": "%pace"}}] * 6
JM_EVENT = {"name": "Easy technique swim 40 min", "type": "Swim", "icu_training_load": 26,
            "workout_doc": {"steps": [{"duration": 480}] + _DRILLS
                            + [{"duration": 1020}, {"duration": 360}]}}


def test_swim_pace_is_time_over_distance_not_the_inverse():
    assert round(aw._swim_pace_s(JM_SWIM), 1) == 103.1          # 1:43, not 97 (= "1:37")
    lines, _ = aw._swim_lines(JM_SWIM, None, 95, None)
    assert lines[0].startswith("Swim average: 1.7 km in 29 min moving = 1:43/100m (+8 s vs CSS 1:35/100m")
    assert "103.1" in lines[0]


def test_prompt_swim_stub_uses_computed_pace():
    p = _prompt()
    assert "distance_m / (duration_s / 100)" not in p
    assert "moving_time_s / (distance_m / 100)" in p
    assert 'COMPUTED FACTS "Swim average" line' in p


def test_swim_blocks_one_line_and_structure_not_executed():
    lines, pd, tiny = aw._activity_facts(JM_SWIM, JM_EVENT, JM_INTERVALS, css_s=95)
    blocks = [l for l in lines if l.startswith("Blocks swum")]
    assert len(blocks) == 1 and "500 m 1:44" in blocks[0] and "never a line per block" in blocks[0]
    assert any("planned structure NOT executed: plan had 6 x 1:00 reps" in l for l in lines)
    assert pd is None and lines[-1] == "PLAN_DELTA: none (planned structure not executed)"


def test_swim_structure_done_keeps_plan_delta():
    reps = [{"type": "WORK", "distance": 50.0, "moving_time": 62}] * 6
    lines, pd, _ = aw._activity_facts(JM_SWIM, JM_EVENT, reps, css_s=95)
    assert pd == "Easy technique swim 40 min|26|13|-50"
    assert not any(l.startswith("Structure:") for l in lines)
    # no rep set planned (one continuous swim): no verdict either way
    assert aw._swim_structure({"workout_doc": {"steps": [{"duration": 2400}]}}, aw._swim_blocks(JM_SWIM, JM_INTERVALS)) is None


def test_structure_rule_one_line_when_not_executed():
    assert "in ONE line" in aw.STRUCTURE_RULE and "never a line per block" in aw.STRUCTURE_RULE


# James's 17 Oct gravel ride: power meter dropped out (15 W avg), both models called it easy.
JM_GRAVEL = {"id": "i198257887", "type": "GravelRide", "moving_time": 8220, "icu_training_load": 31,
             "icu_average_watts": 15, "icu_ftp": 267, "power_load": 31, "hr_load": 124,
             "decoupling": -6.9,
             "icu_zone_times": [{"id": "Z1", "secs": 7684}, {"id": "Z2", "secs": 331},
                                {"id": "Z3", "secs": 130}, {"id": "Z4", "secs": 64}],
             "icu_hr_zone_times": [2582, 3521, 1454, 619, 44, 0, 0]}


def test_broken_power_uses_hr_zones_and_drops_decoupling():
    lines, _, _ = aw._activity_facts(JM_GRAVEL)
    assert lines[0].startswith("POWER DATA BROKEN: avg 15 W is 6% of FTP 267 W")
    assert "heart-rate Load is 124" in lines[0]
    assert lines[1].startswith("Zone split (HR zones") and "Z1-Z2 74%" in lines[1]
    assert lines[2].startswith("Decoupling: not available")
    assert aw._power_broken(RIDE) == ""                          # real power left alone


def test_hr_only_level_gets_hr_zones():
    assert aw._zone_split_line(RIDE, hr_only=True).startswith("Zone split (HR zones")
    assert aw._zone_split_line(RIDE).startswith("Zone split (power zones")


def test_recent_fuel_computed_from_session_log():
    log = [{"activity_id": "new", "sport": "Ride", "duration_min": 119, "nutrition_g_carb": None},
           {"sport": "Ride", "duration_min": 207, "nutrition_g_carb": 165},
           {"sport": "Run", "duration_min": 120, "nutrition_g_carb": 60},
           {"sport": "Ride", "duration_min": 294.2, "nutrition_g_carb": 437},
           {"sport": "Ride", "duration_min": 60, "nutrition_g_carb": 30},
           {"sport": "Ride", "duration_min": 112, "nutrition_g_carb": 80},
           {"sport": "VirtualRide", "duration_min": 230.08, "nutrition_g_carb": 291},
           {"sport": "Ride", "duration_min": 183.9, "nutrition_g_carb": 165}]
    assert aw._recent_fuel(log) == "64 g/hr over the last 4 rides over 90 min"   # Jamie, 17 Oct
    assert aw._recent_fuel([]) == "none logged on recent rides over 90 min"
    p = _prompt(recent_fuel="64 g/hr over the last 4 rides over 90 min")
    assert "(recent avg: 64 g/hr over the last 4 rides over 90 min ·" in p
    assert "[avg g/hr from last 4 rides" not in p


def test_decoupling_entry_only_for_three_hour_rides():
    fred = {"id": "i198246291", "type": "Ride", "moving_time": 124 * 60, "decoupling": 0.5,
            "icu_intensity": 83, "icu_training_load": 143, "start_date_local": "2026-10-17T08:00:00"}
    assert aw._decoupling_entry(fred, 0.5) is None
    long = {**fred, "moving_time": 200 * 60}
    e = aw._decoupling_entry(long, 0.5)
    assert e["duration_min"] == 200 and e["if"] == 0.83 and e["decoupling_pct"] == 0.5
    assert aw._decoupling_entry(long, None) is None
    assert aw._decoupling_entry(long, 0.5, hr_trusted=False) is None


def test_target_activity_picked_in_code():
    acts = [{"id": "i198283108", "type": "Ride", "start_date_local": "2026-10-16T14:47:01",
             "moving_time": 3793},
            {"id": "i198283370", "type": "VirtualRide", "start_date_local": "2026-10-17T10:21:00",
             "moving_time": 5398}]
    assert aw._pick_target(["i198283108", "i198283370"], acts) == "i198283370"
    p = _prompt(target_id="i198283370")
    assert "Step 3 — Debrief activity i198283370 (picked in code" in p
    assert "Duplicate upload guard:" in p


def test_no_next_time_line_with_over_plan():
    assert "Never one for an activity with an OVER PLAN line" in _prompt()
