"""Tests for lib/watchdog_checks.py. Run: python3 -m pytest ClaudeCoach/lib/test_watchdog_checks.py"""
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import watchdog_checks as wc

TODAY = date(2026, 10, 2)          # a Friday


def day(n):
    return (TODAY - timedelta(days=n)).isoformat()


def well(n, **kw):
    return {"id": day(n), **kw}


def act(n, typ="Ride", **kw):
    return {"id": f"i{n}{typ}", "start_date_local": day(n) + "T07:00:00", "type": typ, **kw}


def ev(n, typ, name, paired=None, eid=None):
    return {"id": eid or f"e{n}{typ}", "start_date_local": day(n) + "T00:00:00", "type": typ,
            "name": name, "paired_activity_id": paired}


def test_t1_needs_three_days_in_a_row():
    rows = [well(3, ctl=60, atl=70), well(2, ctl=60, atl=90), well(1, ctl=60, atl=90), well(0, ctl=60, atl=90)]
    f = wc.t1_atl_over_ctl(rows)
    assert f and f["trigger"] == "T1" and "3 days" in f["signal"]
    assert wc.t1_atl_over_ctl(rows[:-1]) is None


def test_t3_hrv_week_on_week():
    rows = [well(n, hrv=40) for n in range(0, 7)] + [well(n, hrv=50) for n in range(7, 14)]
    f = wc.t3_hrv_down(rows, TODAY)
    assert f and "-20%" in f["signal"]
    assert wc.t3_hrv_down([well(n, hrv=48) for n in range(14)], TODAY) is None


def test_t4_6h57_is_not_a_short_night():
    rows = [well(n, sleepSecs=25020) for n in (1, 3, 5)]          # 6.95 h
    assert wc.t4_short_sleep(rows, TODAY) is None


def test_t4_three_short_nights():
    rows = [well(n, sleepSecs=6 * 3600) for n in (1, 3, 5)] + [well(n, sleepSecs=8 * 3600) for n in (0, 2, 4, 6)]
    f = wc.t4_short_sleep(rows, TODAY)
    assert f and f["items"] == sorted([day(1), day(3), day(5)])
    assert wc.t4_short_sleep(rows[1:], TODAY) is None


def test_t5_counts_unmatched_workouts_and_accepts_a_treadmill_run():
    # Jamie, 2 Oct 2026: 29 Sep swim, 30 Sep strength and 1 Oct ride missed; the 29 Sep
    # planned run was done on the treadmill (VirtualRun, unpaired).
    events = [ev(3, "Swim", "Technique swim"), ev(3, "Run", "Easy run"),
              ev(2, "WeightTraining", "Strength A"), ev(1, "Ride", "Endurance spin"),
              ev(5, "Ride", "Easy Z2 ride", paired="i5Ride"), ev(0, "Run", "Today's run")]
    acts = [act(3, "VirtualRun"), act(5, "Ride"), act(1, "Run"), act(1, "Swim")]
    f = wc.t5_missed(events, acts, TODAY)
    assert f["trigger"] == "T5" and "3 planned sessions" in f["signal"]
    assert f["items"] == sorted(["e3Swim", "e2WeightTraining", "e1Ride"])


def test_t5_one_miss_is_quiet():
    assert wc.t5_missed([ev(2, "Run", "Run")], [], TODAY) is None


def test_t6_only_z2_rides_with_trusted_hr():
    acts = [act(2, decoupling=8.0, icu_intensity=68, name="Z2", moving_time=5400),
            act(3, decoupling=9.0, icu_intensity=85, name="Tempo", moving_time=5400),
            act(4, decoupling=7.0, icu_intensity=70, name="Bad strap", moving_time=5400),
            act(5, decoupling=5.6, icu_intensity=50, name="Errands", moving_time=1500)]
    f = wc.t6_decoupling(acts, TODAY, untrusted={"i4Ride"})
    assert f["items"] == ["i2Ride"]


def test_t7_t8_protocol_window():
    heat = {"active": True, "starts": day(10)}
    log = [{"date": day(9), "dose": 0.7}, {"date": day(8)}]           # no dose field counts 1.0
    out = wc.t7_t8_heat(heat, log, TODAY, 2.0, 3.0)
    assert [f["trigger"] for f in out] == ["T7", "T8"]
    assert "1.70" in out[0]["signal"]


def test_t7_before_start_only_with_maintenance():
    heat = {"active": True, "starts": (TODAY + timedelta(days=30)).isoformat()}
    assert wc.t7_t8_heat(heat, [], TODAY, 2.0, 3.0) == []
    assert wc.t7_t8_heat({**heat, "maintenance": True}, [], TODAY, 2.0, 3.0)[0]["tier"] == 2
    assert wc.t7_t8_heat({**heat, "silent": True, "maintenance": True}, [], TODAY, 2.0, 3.0) == []


def test_t10_run_km_week_on_week():
    # Fri 2 Oct: this week Mon 28 Sep - Fri; last week Mon 21 - Sun 27 Sep
    acts = [act(4, "Run", distance=15000), act(1, "Run", distance=12000),
            act(8, "Run", distance=12600)]
    f = wc.t10_run_km(acts, TODAY)
    assert f and "27.0 km vs 12.6 km" in f["signal"]
    assert wc.t10_run_km([act(1, "Run", distance=5000)], TODAY) is None     # no runs last week


def test_t10_a_post_race_recovery_week_is_not_the_baseline():
    # James, 3 Oct 2026: 6.5 km recovery week after a 41.3 km Ironman marathon. 12.1 km
    # this week fired "+86%"; against the 4-week average (21.3 km) it is a step down.
    acts = [act(1, "Run", distance=7000), act(4, "Run", distance=5100),          # this week
            act(7, "Run", distance=6500),                                        # recovery
            act(13, "Run", distance=41300), act(16, "Run", distance=5400),       # race week
            act(22, "Run", distance=15600), act(29, "Run", distance=16400)]
    assert wc.t10_run_km(acts, TODAY) is None
    # A real jump over the 4-week average still fires, and says what it was measured against.
    f = wc.t10_run_km(acts + [act(2, "Run", distance=20000)], TODAY)
    assert f and "4-week average" in f["signal"]


def test_t11_both_completed_weeks_below_target():
    acts = [act(8, "WeightTraining"), act(15, "WeightTraining")]
    assert wc.t11_strength(acts, TODAY, 2)["signal"].startswith("strength 1 and 1")
    assert wc.t11_strength(acts + [act(9, "WeightTraining")], TODAY, 2) is None
    assert wc.t11_strength(acts, TODAY, None) is None


def test_suppression_only_drops_items_already_logged(tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "BASE", tmp_path)
    (tmp_path / "athletes" / "x").mkdir(parents=True)
    f = {"trigger": "T5", "tier": 1, "items": ["a", "b"], "signal": "s"}
    assert wc.new_only([f], wc.load_state("x"), TODAY) == [f]
    wc.record("x", [f], TODAY - timedelta(days=1))
    assert wc.new_only([f], wc.load_state("x"), TODAY) == []
    g = {**f, "items": ["a", "b", "c"]}                      # a new missed session
    assert wc.new_only([g], wc.load_state("x"), TODAY) == [g]
    assert wc.new_only([f], wc.load_state("x"), TODAY + timedelta(days=3)) == [f]   # past 3 days


def test_t9b_fingerprint_changes_with_the_files(tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "BASE", tmp_path)
    adir = tmp_path / "athletes" / "x"
    (adir / "reference").mkdir(parents=True)
    assert wc.t9b_fingerprint("x") is None
    (adir / "reference" / "decision-points.md").write_text("B7b gearing by mid-May")
    (adir / "current-state.json").write_text('{"open_actions": []}')
    a = wc.t9b_fingerprint("x")
    (adir / "current-state.json").write_text('{"open_actions": [{"action": "gearing"}]}')
    assert a and wc.t9b_fingerprint("x") != a
