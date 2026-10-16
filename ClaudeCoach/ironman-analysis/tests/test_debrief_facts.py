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
