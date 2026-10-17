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

def test_decoupling_line_is_the_corrected_figure_never_the_icu_field():
    # RIDE carries Intervals.icu's raw decoupling (-5.4): the line must never quote it
    assert "not available" in aw._decoupling_line(RIDE)
    assert "-5.4" not in aw._decoupling_line(RIDE)
    assert "not available" in aw._decoupling_line(KATHRYN_RUN)
    ok = {"pct": 3.2, "method": "power:HR", "reason": None}
    assert "not available" in aw._decoupling_line(RIDE, hr_trusted=False, drift_result=ok)


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
    dr = {"pct": -1.5, "method": "power:HR", "reason": None}
    lines, pd, tiny = aw._activity_facts(RIDE, ev, drift_result=dr)
    assert not tiny and pd == "Endurance ride|39|39|0"
    assert lines[-1] == "PLAN_DELTA: Endurance ride|39|39|0"
    assert any(l.startswith("Drift (") and l.endswith("power:HR): -1.5%") for l in lines)
    assert not any("Intervals.icu): -5.4" in l for l in lines)


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
    assert "method" not in e                                       # not known: not written
    assert aw._decoupling_entry(long, 0.5, True, "power:HR")["method"] == "power:HR"


# The corrected drift figure (lib/drift.py), not Intervals.icu's raw decoupling -----------
# Jamie, 17 Oct 2026: "make sure drift uses a good drift metric, takes out hills, freewheeling
# etc". Stub clients and 1 Hz streams: nothing here touches Intervals.icu.

import drift as drift_lib                                            # noqa: E402


def _ride_streams(n=7200, watts=200.0, hr0=130.0, hr1=140.0):
    return [{"type": "watts", "data": [watts] * n},
            {"type": "heartrate", "data": [hr0 + (hr1 - hr0) * i / (n - 1) for i in range(n)]},
            {"type": "velocity_smooth", "data": [8.0] * n}]


def _long_ride(**kw):
    return {"id": "i1", "type": "Ride", "name": "Long ride", "moving_time": 7200,
            "start_date_local": "2026-10-17T08:00:00", "icu_training_load": 120,
            "icu_average_watts": 200, "icu_weighted_avg_watts": 200, "icu_ftp": 250,
            "decoupling": -9.9, **kw}                               # -9.9: Intervals.icu's raw figure


class _Client:
    def __init__(self, acts, streams=None, fail=False):
        self.acts, self.streams, self.fail, self.fetched = acts, streams, fail, []

    def get_training_history(self, days=3):
        return self.acts

    def get_activity_streams(self, aid):
        self.fetched.append(aid)
        if self.fail:
            raise RuntimeError("boom: ICU 500")
        return self.streams


def _facts(monkeypatch, tmp_path, client, untrusted=()):
    cfg = tmp_path / "athletes.json"
    cfg.write_text(json.dumps({"x": {"icu_athlete_id": "1", "icu_api_key": "k"}}))
    monkeypatch.setattr(aw, "ATHLETES_CONFIG", cfg)
    monkeypatch.setitem(sys.modules, "icu_api", types.SimpleNamespace(IcuClient=lambda *a: client))
    monkeypatch.setattr(aw.hr_quality, "is_untrusted", lambda slug, aid: aid in untrusted)
    return aw._facts_pass("x", tmp_path / "session-log.json")


def test_long_ride_meta_carries_the_corrected_pct_not_icus(monkeypatch, tmp_path):
    ride, streams = _long_ride(), _ride_streams()
    want = drift_lib.session_drift(ride, streams)
    assert want["pct"] is not None and want["pct"] > 0               # HR rose 130 -> 140 at 200 W
    client = _Client([ride], streams)
    block, meta = _facts(monkeypatch, tmp_path, client)
    assert meta["i1"]["decoupling"] == want["pct"] != ride["decoupling"]
    assert meta["i1"]["drift"] == want and want["method"] == "power:HR"
    assert client.fetched == ["i1"]                                  # streams fetched once
    assert f"power:HR): {want['pct']:+.1f}%" in block and "-9.9" not in block


def test_decoupling_log_entry_gets_the_corrected_figure_and_method(monkeypatch, tmp_path):
    ride = _long_ride(id="i2", moving_time=12000, icu_intensity=70)   # 200 min
    streams = _ride_streams(n=12000)
    _, meta = _facts(monkeypatch, tmp_path, _Client([ride], streams))
    m = meta["i2"]
    entry = aw._decoupling_entry(m["activity"], m["decoupling"], True, m["drift"]["method"])
    assert entry["decoupling_pct"] == round(drift_lib.session_drift(ride, streams)["pct"], 1)
    assert entry["decoupling_pct"] != -9.9 and entry["method"] == "power:HR"
    assert entry["duration_min"] == 200


def test_debrief_line_states_figure_and_method():
    line = aw._decoupling_line(RIDE, True, {"pct": 3.2, "method": "power:HR", "reason": None})
    assert line == ("Drift (warm-up and cool-down cut, coasting and stops removed, "
                    "power:HR): +3.2%")
    gap = aw._decoupling_line(RUN, True, {"pct": -0.8, "method": "grade-adjusted pace:HR",
                                          "reason": None})
    assert gap.endswith("grade-adjusted pace:HR): -0.8%")
    assert aw._decoupling_line(RUN, True, {"pct": 0.0, "method": "pace:HR",
                                           "reason": None}).endswith("pace:HR): +0.0%")


def test_no_figure_line_gives_the_reason_and_still_says_leave_it_out():
    line = aw._decoupling_line(RIDE, True, {"pct": None, "method": None,
                                            "reason": "intervals or surges (power too variable)"})
    assert "not available (intervals or surges (power too variable))" in line
    assert line.endswith("leave decoupling and HR drift out of this debrief entirely")
    bare = aw._decoupling_line(RIDE, True, {"pct": None, "method": None, "reason": None})
    assert bare == ("Decoupling: not available - leave decoupling and HR drift out of this "
                    "debrief entirely")
    assert aw._decoupling_line(RIDE, True, None) == bare


def test_surgy_ride_has_no_figure_and_the_block_says_why(monkeypatch, tmp_path):
    ride = _long_ride(icu_weighted_avg_watts=300)                    # VI 1.5: intervals
    client = _Client([ride], _ride_streams())
    block, meta = _facts(monkeypatch, tmp_path, client)
    assert meta["i1"]["decoupling"] is None
    assert meta["i1"]["drift"]["reason"] == "intervals or surges (power too variable)"
    assert "not available (intervals or surges (power too variable)) - leave decoupling" in block


def test_stream_fetch_error_gives_no_figure_and_no_crash(monkeypatch, tmp_path):
    client = _Client([_long_ride()], fail=True)
    block, meta = _facts(monkeypatch, tmp_path, client)
    assert "i1" in meta and meta["i1"]["decoupling"] is None         # not the outer fail-soft ("", {})
    assert meta["i1"]["drift"]["pct"] is None and meta["i1"]["drift"]["reason"]
    assert "leave decoupling and HR drift out of this debrief entirely" in block
    assert client.fetched == ["i1"]


def test_short_activity_fetches_no_streams(monkeypatch, tmp_path):
    ride = _long_ride(moving_time=1800)                              # 30 minutes
    run = {"id": "i3", "type": "Run", "name": "Short run", "moving_time": 2399,
           "start_date_local": "2026-10-17T07:00:00", "decoupling": 4.0}
    client = _Client([ride, run], _ride_streams())
    block, meta = _facts(monkeypatch, tmp_path, client)
    assert client.fetched == []
    assert meta["i1"]["decoupling"] is None and meta["i3"]["decoupling"] is None
    assert "4.0" not in block and "-9.9" not in block


def test_no_fetch_when_hr_untrusted_or_power_broken(monkeypatch, tmp_path):
    broken = _long_ride(id="i4", icu_average_watts=15, power_load=31, hr_load=124)
    bad_hr = _long_ride(id="i5")
    client = _Client([broken, bad_hr], _ride_streams())
    block, meta = _facts(monkeypatch, tmp_path, client, untrusted=("i5",))
    assert client.fetched == []
    assert meta["i4"]["decoupling"] is None and meta["i5"]["decoupling"] is None
    assert meta["i5"]["drift"]["reason"] == "heart rate failed the check"


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


# Length by level, no double asks, short over-plan line, no template leaks ----------------
# (Jamie, 17 Oct 2026: "This is way too wordy for Fred", a MID debrief for a 2h04 ride.)

FRED_DEBRIEF = (
    "Long ride, 51 km in 2h04, but only 54% of it was in the easy zones, so it ran harder "
    "than the plan's easy label.\n"
    "Load 143, which is well over the 70 planned for this ride. Average power was 136 W, with "
    "46% of the time in Zone 3 or above and 31% in Zone 4 or above.\n"
    "Heart-rate drift was 0.5%. Average heart rate was 142 bpm.\n"
    "Most of the time above 150 bpm came on short climbs.\n"
    "Nutrition: g carbs/hr, bottles, and sodium? (recent avg: 50 g/hr · your agreed training "
    "figure 45 g/hr)")


def _body(text):
    lines = [l for l in text.split("\n") if l.strip()]
    while lines and "?" in lines[-1]:
        lines.pop()
    return lines


def test_cap_per_level_drops_whole_trailing_lines():
    body = "\n".join(f"Line {i} has exactly six words." for i in range(1, 8)) + "\nHow did it feel?"
    for level, (n, w) in {"beginner": (2, 40), "mid": (3, 60), "pro": (5, 100)}.items():
        out, dropped = aw._cap_debrief(body, level)
        kept = _body(out)
        assert kept == [f"Line {i} has exactly six words." for i in range(1, n + 1)], level
        assert len(dropped) == 7 - n
        assert out.endswith("How did it feel?")           # the question is not counted
    assert aw._debrief_cap("unknown") == (3, 60)


def test_cap_by_words_never_cuts_mid_sentence_and_keeps_verdict():
    long = "word " * 45
    out, dropped = aw._cap_debrief(f"Verdict sentence here.\n{long.strip()}\nShort.", "beginner")
    # Last first, until under the cap: the trailing short line goes before the long one.
    assert out == "Verdict sentence here."
    assert dropped == [long.strip(), "Short."]
    # The verdict alone over budget is kept whole, never truncated.
    out, dropped = aw._cap_debrief(long.strip(), "beginner")
    assert out == long.strip() and dropped == []


def test_cap_protects_over_plan_line_and_rep_blocks():
    text = "Verdict.\nLoad 143.\nAvg HR 142.\nBig day out - keeping to plan lets Tuesday's threshold land well."
    out, _ = aw._cap_debrief(text, "mid", protect_last=True)
    assert _body(out) == ["Verdict.", "Load 143.",
                          "Big day out - keeping to plan lets Tuesday's threshold land well."]
    reps = "Verdict.\nRep 1: 4:00 · 300 W\nRep 2: 4:00 · 305 W\nRep 3: 4:00 · 310 W\nLoad 80."
    out, _ = aw._cap_debrief(reps, "beginner")              # rep run is one unit
    assert _body(out) == ["Verdict.", "Rep 1: 4:00 · 300 W", "Rep 2: 4:00 · 305 W",
                          "Rep 3: 4:00 · 310 W"]


def test_fred_debrief_comes_out_within_mid_cap(monkeypatch):
    monkeypatch.setattr(aw.coaching_prefs, "fuelling_on", lambda slug: True)
    entry = {"activity_id": "i1", "sport": "Ride", "duration_min": 124, "rpe": None,
             "nutrition_g_carb": None}
    text, scrubbed = aw._scrub_templates(FRED_DEBRIEF)
    assert "g carbs/hr" not in text and scrubbed == []
    kinds = aw._card_kinds(entry, "fred", [], history=[])
    assert kinds == [aw.ask_gate.RPE, aw.ask_gate.NUTRITION]
    text = aw._drop_card_questions(text, kinds)
    assert "carbs" not in text and "?" not in text
    out, dropped = aw._cap_debrief(text, "mid")
    lines = _body(out)
    assert len(lines) <= 3 and len(out.split()) <= 60
    assert lines[0].startswith("Long ride, 51 km in 2h04")
    assert dropped


def test_no_carbs_or_effort_question_when_card_sent(monkeypatch):
    monkeypatch.setattr(aw.coaching_prefs, "fuelling_on", lambda slug: True)
    ride = {"activity_id": "i1", "sport": "Ride", "duration_min": 124}
    kinds = aw._card_kinds(ride, "fred", [], history=[])
    text = ("Steady ride.\nLoad 90.\nRoughly how many grams of carbs an hour did you take on?"
            " (recent avg: 50 g/hr)")
    assert aw._drop_card_questions(text, kinds) == "Steady ride.\nLoad 90."
    assert aw._drop_card_questions("Easy run.\nHow did it feel?",
                                   [aw.ask_gate.RPE]) == "Easy run."
    assert aw._drop_card_questions("Easy run.\nInjury pain score during? (0-10)",
                                   [aw.ask_gate.ANKLE]) == "Easy run."
    # A short ride's card has no carb row, so the text keeps a carbs ask.
    assert "carbs" in aw._drop_card_questions("Ride.\nRoughly how many grams of carbs an hour?",
                                              [aw.ask_gate.RPE])
    # A question the card does not cover stays.
    q = "Cardio, 30 min.\nWhat was this one - a run, a ride, or something else?"
    assert aw._drop_card_questions(q, [aw.ask_gate.RPE]) == q


def test_no_card_means_the_text_question_stays(monkeypatch):
    monkeypatch.setattr(aw.coaching_prefs, "fuelling_on", lambda slug: True)
    assert aw._card_kinds({"sport": "Ride", "duration_min": 10}, "x", [], history=[]) == []
    assert aw._card_kinds({"sport": "Strength", "duration_min": 40, "avg_hr": 110},
                          "x", [], history=[]) == []
    answered = {"sport": "Ride", "duration_min": 124, "rpe": 6, "nutrition_g_carb": 60}
    assert aw._card_kinds(answered, "x", [], history=[]) == []
    text = "Ride.\nRoughly how many grams of carbs an hour did you take on?"
    assert aw._drop_card_questions(text, []) == text
    assert aw._card_kinds({"sport": "Run", "duration_min": 40}, "x",
                          [{"location": "knee"}], history=[]) == [aw.ask_gate.ANKLE]


def test_template_text_never_reaches_athlete():
    out, dropped = aw._scrub_templates(
        "Solid ride.\nNutrition — g carbs/hr, bottles, and sodium?\n"
        "Rep 1: DUR · XXXw (X% FTP) · AVGbpm/MAXbpm\nHeader: NxDUR @ AVG W\n"
        "Fuel: [avg g/hr from last 4 rides] · {fuel_avg}")
    assert out == "Solid ride.\nRoughly how many grams of carbs an hour did you take on?"
    assert len(dropped) == 3
    real = "Reps: 4:12, 4:10, 4:08/km · Load 62 · 60 g carbs/hr logged"
    assert aw._scrub_templates(real) == (real, [])
    p = _prompt()
    assert "Nutrition — g carbs/hr" not in p and "g carbs/hr, bottles" not in p


def test_prompt_states_level_budget_and_verdict_first():
    for level, (n, w) in {"beginner": (2, 40), "mid": (3, 60), "pro": (5, 100)}.items():
        p = aw._build_prompt("t", "Fred", 250, [], {"coaching_level": level})
        assert f"ANALYSIS is at most {n} lines and about {w} words" in p
        assert "Line 1 = ONE plain verdict sentence" in p
    assert "Never stack stats" in aw._length_block("mid")
    assert "effort words" in aw._length_block("beginner")


def test_over_plan_line_is_short_credit_first(monkeypatch, tmp_path):
    import consistency
    acts = [{"id": "i5", "name": "Long ride", "start_date_local": f"{aw.date.today()}T09:00:00",
             "type": "Ride", "paired_event_id": 1}]
    evs = [{"id": 1, "name": "Endurance", "start_date_local": f"{aw.date.today()}T00:00:00",
            "type": "Ride"},
           {"id": 2, "name": "Threshold", "type": "Ride",
            "start_date_local": f"{aw.date.today() + aw.timedelta(days=2)}T00:00:00"}]

    class C:
        def __init__(self, *a): pass
        def get_training_history(self, days): return acts
        def get_events(self, a, b): return evs
    monkeypatch.setitem(sys.modules, "icu_api", types.SimpleNamespace(IcuClient=C))
    monkeypatch.setattr(consistency, "overshoot", lambda plan, a: True)
    cfg = tmp_path / "a.json"
    cfg.write_text(json.dumps({"fred": {"icu_athlete_id": "x", "icu_api_key": "y"}}))
    monkeypatch.setattr(aw, "ATHLETES_CONFIG", cfg)
    out = aw._overshoot_pass("fred", tmp_path / "none.json")
    assert "(i5)" in out and "Threshold" in out
    assert "15 words or fewer" in out and "credit the effort first" in out
    assert "because steady sessions build" not in out and "No 'because'" in out
    example = "Big day out - keeping to plan lets Tuesday's threshold land well."
    assert example in out and len(example.split()) <= 15
