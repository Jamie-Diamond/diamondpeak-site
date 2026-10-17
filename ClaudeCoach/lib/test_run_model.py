"""Tests for lib/run_model.py and lib/performance.py. Run:
python3 -m pytest ClaudeCoach/lib/test_run_model.py"""
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import performance  # noqa: E402
import run_model as rm  # noqa: E402

FENIX = "Garmin fenix 8"
_ids = iter(range(1, 10_000))


def act(when, km, secs, hr=140, type_="Run", race=False, name="Run", elapsed=None,
        device=FENIX, gap=None):
    """One Intervals.icu activity as the API returns it (only the fields the model reads)."""
    return {"id": f"i{next(_ids)}", "start_date_local": when, "type": type_, "name": name,
            "distance": km * 1000, "moving_time": secs, "elapsed_time": elapsed or secs,
            "average_heartrate": hr, "athlete_max_hr": 190, "icu_resting_hr": 55,
            "average_speed": km * 1000 / secs, "gap": gap, "device_name": device, "race": race}


def easy_runs(start: date, end: date, every=2):
    out, d = [], start
    while d <= end:
        out.append(act(f"{d}T07:00:00", 10.0, 3000, hr=140))        # 5:00/km
        d += timedelta(days=every)
    return out


def jamie_like():
    """Jamie's real shape: a 5k parkrun PB, a half, a flagged 10k; plus the efforts that
    must NOT count - a hot long run and a Z2 marathon at 'racing' HR, and a 10k off the bike."""
    acts = easy_runs(date(2024, 10, 1), date(2026, 10, 16))
    acts += [
        act("2025-03-23T09:05:00", 21.30, 5563, hr=167, name="Hampton Court Half"),
        act("2025-12-27T09:01:00", 5.01, 1124, hr=171, name="Gosport Running"),     # Saturday
        act("2025-06-08T10:30:00", 10.01, 2474, hr=180, race=True, name="Fareham 10k"),
        act("2026-07-17T09:17:00", 21.24, 6740, hr=166, name="Another 10%"),          # hot, 5:17/km
        act("2025-04-27T09:00:00", 42.56, 13990, hr=145, name="Solid z2"),
        act("2026-07-26T07:40:00", 1.55, 1700, hr=138, type_="OpenWaterSwim", name="Tri swim"),
        act("2026-07-26T08:15:00", 37.3, 3740, hr=150, type_="Ride", name="Tri bike"),
        act("2026-07-26T09:20:00", 9.93, 2497, hr=165, name="Tri run"),
    ]
    return acts


def test_vdot_riegel_and_vickers_by_hand():
    assert 53.5 < rm.vdot(5000, 1117) < 55.0                  # 18:37 5k ~ VDOT 54
    assert round(rm.riegel(1117, 5000, 10000, 1.06)) == 2329  # 1117 x 2^1.06
    # Model 2, Jamie's 5k / half (k 1.115) at 24.7 miles a week: 3:25:54 by hand
    assert abs(rm.vickers_model2(5563, 21097.5, 1.115, 24.7) - 12354) < 15
    # Model 1 from the half at 40 miles a week: 3:25:42 by hand
    assert abs(rm.vickers_model1(5563, 21097.5, 40) - 12342) < 15


def test_detect_races_keeps_races_and_drops_hot_long_runs_z2_and_tri_legs():
    races = rm.detect_races(jamie_like())
    got = {(r["date"], r["event"], r["source"]) for r in races}
    assert got == {("2025-03-23", "half_marathon", "racing heart rate"),
                   ("2025-06-08", "10k", "race flag"),
                   ("2025-12-27", "5k", "parkrun")}
    half = next(r for r in races if r["event"] == "half_marathon")
    assert half["time_s"] == 5563 and half["vdot"] > 45     # GPS 21.3 km: time not scaled


def test_coach_recorded_result_beats_the_watch_and_exclusions_hold():
    acts = jamie_like()
    flagged = next(a for a in acts if a["name"] == "Fareham 10k")
    races = rm.detect_races(acts, overrides=[{"date": "2025-12-27", "event": "5k", "time": "18:37",
                                              "name": "Gosport parkrun (official)"}],
                            exclude=[flagged["id"]])
    five = [r for r in races if r["event"] == "5k"]
    assert len(five) == 1 and five[0]["time_s"] == 1117 and five[0]["source"] == "manual"
    assert not any(r["event"] == "10k" for r in races)


def test_personal_k_comes_from_close_pairs_and_shrinks_to_default():
    today = date(2026, 10, 17)
    pair = [{"date": "2026-05-01", "event": "5k", "dist_m": 5000.0, "time_s": 1200},
            {"date": "2026-05-15", "event": "10k", "dist_m": 10000.0, "time_s": 2600}]
    k = rm.personal_k(pair, today)
    raw = __import__("math").log(2600 / 1200) / __import__("math").log(2)       # 1.115
    assert k["own"] and k["pairs"] == 1 and rm.DEFAULT_K < k["k"] < raw
    assert rm.personal_k(pair[:1], today) == {"k": rm.DEFAULT_K, "pairs": 0, "own": False}


def test_predict_from_races_uses_vickers_for_the_marathon():
    today = date(2026, 10, 17)
    races = rm.detect_races(jamie_like())
    five = rm.predict(5000, races, today, 40)
    assert five["method"] == "races" and five["model"] == "riegel"
    assert 1100 < five["now_s"] < 1200                     # near the 18:44 parkrun
    assert "From your races: 5k 18:44 (Dec 2025)" in five["note"]
    mara = rm.predict(42195, races, today, 40)
    assert mara["model"] == "vickers2" and "40 km a week" in mara["note"]
    riegel_only = rm.riegel(five["now_s"], 5000, 42195, five["k"])
    assert mara["now_s"] > riegel_only                     # Vickers: Riegel is too kind
    more_km = rm.predict(42195, races, today, 90)
    assert more_km["now_s"] < mara["now_s"]                # volume helps the marathon


def test_predict_falls_back_to_training_then_to_nothing():
    today = date(2026, 10, 17)
    assert rm.predict(5000, [], today, 30) is None
    old = [{"date": "2022-01-01", "event": "5k", "dist_m": 5000.0, "time_s": 1200, "vdot": 50}]
    assert rm.predict(5000, old, today, 30) is None        # older than two years
    training = [{"event": "5k", "dist_m": 5000.0, "time_s": 1260, "date": "2026-09-01"},
                {"event": "3k", "dist_m": 3000.0, "time_s": 330, "date": "2026-09-02"}]  # glitch
    p = rm.predict(10000, old, today, 30, training)
    assert p["method"] == "training" and "best hard training effort (5k 21:00)" in p["note"]
    m = rm.predict(42195, [], today, 30, training)
    assert m["model"] == "vickers1" and m["now_s"] > rm.riegel(1260, 5000, 42195, rm.DEFAULT_K)


def test_efficiency_needs_enough_runs_on_one_watch():
    acts = easy_runs(date(2026, 9, 1), date(2026, 10, 16))
    e = rm.efficiency(acts, date(2026, 10, 17))
    assert e["device"] == "garminfenix8" and e["n"] >= rm.EFF_MIN_RUNS and 40 < e["value"] < 70
    assert rm.efficiency(acts[:3], date(2026, 10, 17)) is None


def test_training_efforts_from_the_pace_curve_reply():
    reply = {"list": [{"distance": [3000.0, 5000.0, 10000.0], "values": [700, 1237, 2847],
                       "activity_id": ["i1", "i1", "i2"],
                       "paceModels": [{"type": "CS", "criticalSpeed": 3.98}]}],
             "activities": {"i1": {"start_date_local": "2026-09-30T08:00:00"}}}
    out, cs = performance.training_efforts(reply)
    assert [e["event"] for e in out] == ["3k", "5k", "10k"] and out[1]["date"] == "2026-09-30"
    assert cs == 3.98
    assert performance.training_efforts(None) == ([], None)


class FakeClient:
    def __init__(self, acts):
        self.acts, self.calls = acts, []

    def get_training_history(self, days=30, sport=None):
        self.calls.append(days)
        cut = (date(2026, 10, 17) - timedelta(days=days)).isoformat()
        return [a for a in self.acts if a["start_date_local"][:10] >= cut]

    def _get(self, path, params=None):
        return {"list": []}


def test_activities_cache_tops_up_and_refetches_weekly(tmp_path, monkeypatch):
    monkeypatch.setattr(performance, "BASE", tmp_path)
    (tmp_path / "athletes" / "x").mkdir(parents=True)
    acts = jamie_like()
    c = FakeClient(acts)
    today = date(2026, 10, 17)
    first = performance.activities("x", c, today)
    assert c.calls == [performance.HISTORY_DAYS] and len(first) == len(acts)
    assert set(first[0]) <= set(performance.SLIM)
    c.acts = acts + [act("2026-10-17T07:00:00", 8.0, 2400)]
    second = performance.activities("x", c, today)
    assert c.calls[-1] == performance.OVERLAP_DAYS and len(second) == len(acts) + 1
    performance.activities("x", c, today + timedelta(days=performance.FULL_EVERY_DAYS))
    assert c.calls[-1] == performance.HISTORY_DAYS


def test_build_publishes_predictions_and_keeps_one_row_a_week(tmp_path, monkeypatch):
    monkeypatch.setattr(performance, "BASE", tmp_path)
    (tmp_path / "athletes" / "x").mkdir(parents=True)
    c = FakeClient(jamie_like())
    out = performance.build("x", c, {}, {}, today=date(2026, 10, 17))
    run = out["run"]
    assert set(run["predictions"]) == {"5k", "10k", "half_marathon", "marathon"}
    assert run["k"]["own"] and len(run["races"]) == 3 and run["weeklyKm"] > 0
    performance.build("x", c, {}, {}, today=date(2026, 10, 18))
    rows = json.loads((tmp_path / "athletes" / "x" / "performance-history.json").read_text())
    assert len(rows) == 1 and rows[0]["date"] == "2026-10-18" and rows[0]["pred"]["5k"]


def test_goals_page_uses_the_athletes_races(monkeypatch):
    import planning_pause
    import races
    import season_view as sv
    monkeypatch.setattr(planning_pause, "is_paused", lambda slug, cfg=None: False)
    monkeypatch.setattr(races, "load_races", lambda slug, config=None: [
        {"name": "Parkrun", "date": "2026-11-14", "priority": "C", "distance": "5k"},
        {"name": "Brighton Marathon", "date": "2027-04-04", "priority": "A", "distance": "Marathon"}])
    today = date(2026, 10, 17)
    run = performance.determinants(jamie_like(), today)
    recent = [{"date": f"2026-09-{d:02d}", "sport": "Run", "dist": 10.0, "dur": 50} for d in range(1, 30, 2)]
    p = sv.payload("x", {"race_goal": "sub 3"}, {"run_threshold_pace_per_km": "4:02"},
                   {"recent": recent, "performance": {"run": run}}, today=today)
    five, mara = p["races"]
    assert five["method"] == mara["method"] == "races" and mara["model"] == "vickers2"
    assert mara["raceday_s"] < mara["now_s"] and "From your races" in mara["note"]
    assert five["now_s"] - five["raceday_s"] < (mara["now_s"] - mara["raceday_s"]) * 0.2


def test_out_of_date_after_a_drop_in_running_or_a_break():
    today = date(2026, 10, 17)
    base = easy_runs(date(2025, 1, 1), date(2025, 12, 31), every=2)            # ~35 km a week
    races = [act("2025-12-27T09:01:00", 5.01, 1124, hr=171, name="parkrun")]
    # Consistent: still running the same -> current.
    steady = performance.determinants(base + races + easy_runs(date(2026, 1, 1), date(2026, 10, 16)), today)
    assert steady["predictions"]["5k"]["stale"] is None and steady["recentBreakDays"] < 3
    # Much less running now (every 5 days ~14 km a week, under 60% of ~35).
    drop = performance.determinants(base + races + easy_runs(date(2026, 1, 1), date(2026, 10, 16), every=5), today)
    st = drop["predictions"]["5k"]["stale"]
    assert st["reason"] == "volume" and st["km_now"] < 0.6 * st["km_before"]
    # A month off ending a week ago, volume otherwise similar.
    gap = [a for a in base + easy_runs(date(2026, 1, 1), date(2026, 10, 16))
           if not "2026-09-10" <= a["start_date_local"][:10] <= "2026-10-09"]
    brk = performance.determinants(gap + races, today)
    assert brk["recentBreakDays"] >= 21 and brk["predictions"]["5k"]["stale"]["reason"] == "break"
    p = rm.predict(5000, brk["races"], today, 30, None, brk["recentBreakDays"])
    assert p["note"].startswith("Out of date: 4 weeks without running recently.")


def test_the_goals_page_and_coach_carry_the_flag(monkeypatch):
    import planning_pause
    import races
    import season_view as sv
    monkeypatch.setattr(planning_pause, "is_paused", lambda slug, cfg=None: False)
    monkeypatch.setattr(races, "load_races", lambda slug, config=None: [
        {"name": "Parkrun", "date": "2026-11-14", "priority": "C", "distance": "5k"}])
    today = date(2026, 10, 17)
    run = performance.determinants(jamie_like(), today)
    run["recentBreakDays"] = 30
    p = sv.payload("x", {}, {}, {"recent": [], "performance": {"run": run}}, today=today)
    r = p["races"][0]
    assert r["stale"]["reason"] == "break" and r["note"].startswith("Out of date")


def test_a_parkrun_on_a_day_with_a_ride_still_counts_but_a_triathlon_does_not():
    acts = easy_runs(date(2025, 1, 1), date(2025, 12, 31))
    acts += [act("2025-11-01T09:00:00", 5.0, 1150, hr=172, name="parkrun"),           # Saturday
             act("2025-11-01T14:00:00", 60.0, 7200, hr=130, type_="Ride", name="Afternoon ride")]
    assert [r["date"] for r in rm.detect_races(acts)] == ["2025-11-01"]
    acts += [act("2025-11-08T08:00:00", 1.5, 1700, type_="OpenWaterSwim"),
             act("2025-11-08T08:40:00", 40.0, 3700, type_="Ride"),
             act("2025-11-08T09:50:00", 10.0, 2500, hr=170, race=True, name="Tri run")]
    assert [r["date"] for r in rm.detect_races(acts)] == ["2025-11-01"]


# ── What's holding you back (lib/limiters.py) ────────────────────────────────

import limiters as lim  # noqa: E402

FIT = {"now": 55, "lo": 70, "hi": 95}


def run_stats(km=40, vo2=59.3, thr=242, drift=-1.0, longest=32.0, half=6155):
    return {"weeklyKm": km, "engine": {"vo2max": vo2, "source": "Garmin", "n": 9},
            "threshold": {"pace_s_km": thr, "source": "heart rate", "lthr": 170, "hr_top": 146},
            "durability": {"drift_pct": drift, "n": 5, "longest_km": longest},
            "training": [{"event": "5k", "dist_m": 5000.0, "time_s": 1237},
                         {"event": "half_marathon", "dist_m": 21097.5, "time_s": half},
                         {"event": "30k", "dist_m": 30000.0, "time_s": 8808}]}


def test_threshold_pace_for_a_sub3_marathon_is_about_4_01():
    need = lim.threshold_pace_for(rm.vdot(42195, 10800))
    assert 238 <= need <= 244


def test_jamie_today_endurance_not_shown_yet_and_weekly_km_is_never_the_verdict():
    d = lim.diagnose(42195, 10800, "sub 3", run_stats(), FIT, phase_frac=0.70)
    by = {r["key"]: r for r in d["rows"]}
    assert by["fitness"]["status"] == "green" and by["fitness"]["need"] == "~49 by now · 70–95 race day"
    assert by["engine"]["status"] == by["efficiency"]["status"] == by["durability"]["status"] == "green"
    assert by["endurance"]["status"] == "unshown" and by["endurance"]["you"] == "4:52/km for 21.1 km"
    assert d["limiter"] == "endurance"
    assert d["headline"] == ("You have the engine and efficiency for sub 3. Not shown yet: holding "
                             "4:16/km for long. Your fastest 21.1 km in training is 4:52/km.")
    assert d["fix"] == "Race-pace long runs show it."
    assert not any("km a week" in r["need"] for r in d["rows"])          # no volume pass mark


def test_low_fitness_score_names_fitness_with_km_only_as_the_fix():
    d = lim.diagnose(42195, 10800, "sub 3", run_stats(), {"now": 40, "lo": 70, "hi": 95}, phase_frac=0.8)
    assert d["limiter"] == "fitness" and d["headline"].startswith("Fitness is the issue: your running fitness is 40")
    assert d["fix"] == "More running builds it: you're at 40 km a week."


def test_engine_short_names_the_engine():
    d = lim.diagnose(42195, 10800, "sub 3", run_stats(vo2=48, thr=232), FIT, phase_frac=0.7)
    assert d["limiter"] == "engine" and d["headline"].startswith("Your engine is the issue: VO2max 48")


def test_efficiency_is_named_when_it_also_shows_as_drift():
    d = lim.diagnose(42195, 10800, "sub 3", run_stats(thr=262, drift=12), FIT, phase_frac=0.7)
    by = {r["key"]: r for r in d["rows"]}
    assert by["durability"]["short"] > by["efficiency"]["short"]          # drift is the bigger gap
    assert d["limiter"] == "efficiency" and d["headline"].endswith("It also shows up late in your long runs.")


def test_all_shown_says_nothing_is_holding_you_back():
    d = lim.diagnose(42195, 10800, "sub 3", run_stats(half=5380), FIT, phase_frac=0.7)
    assert d["limiter"] is None and d["headline"].startswith("Nothing is holding you back")


def test_missing_measures_are_unknown_not_guessed():
    d = lim.diagnose(10000, 3000, "10k in 50 min", {"weeklyKm": 20}, {"now": 15, "lo": 22, "hi": 36})
    by = {r["key"]: r for r in d["rows"]}
    assert {by[k]["status"] for k in ("engine", "efficiency", "endurance", "durability")} == {"unknown"}
    assert d["limiter"] == "fitness"
    assert lim.diagnose(10000, None, "", {"weeklyKm": 20}, None) is None


def test_goals_payload_carries_the_card_with_the_phase(monkeypatch):
    import planning_pause
    import races
    import season_view as sv
    monkeypatch.setattr(planning_pause, "is_paused", lambda slug, cfg=None: False)
    monkeypatch.setattr(races, "load_races", lambda slug, config=None: [
        {"name": "Brighton Marathon", "date": "2027-04-04", "priority": "A", "distance": "Marathon"}])
    cfg = {"race_goal": "sub 3", "plan_start": "2026-11-02", "race_date": "2027-04-04",
           "phase_tss": {"base_end_week": 6, "build_end_week": 14}}
    p = sv.payload("x", cfg, {}, {"recent": [], "performance": {"run": run_stats()}},
                   today=date(2026, 10, 17))
    L = p["limiters"]
    assert len(L["rows"]) == 5 and L["limiter"] in ("endurance", "fitness")
