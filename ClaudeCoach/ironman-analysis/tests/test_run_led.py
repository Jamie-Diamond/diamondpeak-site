"""Run-led weeks (Jamie, 1 Oct 2026): "the planning could end up pushing me to train more
on the bike when it's not relevant". For a run race the week is built from RUNNING, and
bike / swim only top up to keep total Fitness at the athlete's floor."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import plan_tools as pt  # noqa: E402

BRIGHTON = {"race_name": "Brighton Marathon", "race_date": "2027-04-04", "race_goal": "sub 3",
            "plan_start": "2026-11-02",
            "phase_tss": {"base_end_week": 6, "build_end_week": 14, "specific_end_week": 17,
                          "peak_end_week": 19},
            "ctl_targets": {"maintenance_ctl_band": [65, 75],
                            "phase_ctl": {"base": 76, "build": 86, "specific": 90, "peak": 95},
                            "race_min": 85},
            "max_ctl_ramp_per_week": 4.0}
IN_BASE = date(2026, 11, 9)


def _req(monkeypatch, sc, cfg=BRIGHTON, ctl=78.0):
    monkeypatch.setattr(pt, "sport_ctl_cached", lambda slug, max_age_days=2: sc)
    return pt.required_tss(cfg, ctl, today=IN_BASE, profile={}, slug="x")


def test_the_week_is_built_from_running(monkeypatch):
    r = _req(monkeypatch, {"run": 30.0, "ride": 40.0, "swim": 10.0})
    assert r["run_led"] is True
    assert r["run_weekly_tss"] == round(7 * 30 * 1.15)          # +15% on holding running
    w_floor = pt.compute_required_tss(78.0, 65.0, pt._MAINTENANCE_CONVERGE_WEEKS)
    assert r["cross_training_tss"] == max(0, w_floor - r["run_weekly_tss"])
    assert r["recommended_weekly_tss"] == r["run_weekly_tss"] + r["cross_training_tss"]
    assert r["weekly_tss_floor"] <= r["recommended_weekly_tss"]
    assert "fitness_surplus" not in r                           # total is a floor, not a goal
    assert "Never add bike" in r["note"]


def test_the_total_target_is_not_chased(monkeypatch):
    r = _req(monkeypatch, {"run": 30.0, "ride": 40.0, "swim": 10.0})
    total_line = pt.compute_required_tss(78.0, 76.0, 4)          # what the old way asked
    assert r["recommended_weekly_tss"] < total_line


def test_running_already_high_holds_it(monkeypatch):
    r = _req(monkeypatch, {"run": 80.0, "ride": 10.0, "swim": 0.0}, ctl=90.0)
    assert r["run_weekly_tss"] == round(7 * 80)


def test_no_cache_or_not_a_run_race_is_unchanged(monkeypatch):
    assert "run_led" not in _req(monkeypatch, None)
    tri = dict(BRIGHTON, race_name="70.3 Weymouth", race_goal="5:00")
    assert "run_led" not in _req(monkeypatch, {"run": 30.0, "ride": 40.0, "swim": 10.0}, cfg=tri)


def test_the_builder_closes_running_first_and_caps_bike():
    src = (REPO / "scripts" / "stage1-plan.py").read_text()
    assert 'if brief.get("run_led"):' in src
    assert 'target = _sport_load(_is_run) + (brief.get("cross_training_tss") or 0)' in src


def test_every_target_caller_passes_the_athlete():
    for f, needle in (("lib/session_library.py", "profile=profile, slug=slug"),
                      ("lib/plan_audit.py", "last_week_tss=lw, slug=slug"),
                      ("lib/plan_builder.py", "slug=slug).get(\"weekly_tss_floor\")")):
        assert needle in (REPO / f).read_text(), f
