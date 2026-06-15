"""Tests for the conversational planning maths (lib/plan_tools.py) and the
canonical forward-PMC primitive (primitives/load.project_pmc_daily).

These lock the 15 Jun fix for the chat-path planning failures: deterministic
TSS / weekly roll-up / CTL projection, sharing ONE implementation with the load
chart so the bot can never freelance a training number again.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

# plan_tools lives in ClaudeCoach/lib; the primitives are already importable.
LIB = Path(__file__).resolve().parents[2] / "lib"
TELEGRAM = Path(__file__).resolve().parents[2] / "telegram"
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(TELEGRAM))

import plan_tools as pt                                   # noqa: E402
from primitives.load import project_pmc_daily            # noqa: E402
from primitives.planned_tss import tss_from_segments, render_workout  # noqa: E402


# ── single-source guarantee: CLI projection ≡ the load chart's projection ──────
def test_project_pmc_matches_chart():
    import charts  # the live chart projector, now delegating to the primitive
    days = [{"date": "2999-01-01", "activities": [{"tss": 113}]},
            {"date": "2999-01-02", "activities": [{"tss": 58}]},
            {"date": "2999-01-03", "activities": [{"tss": 55}]}]
    chart_tsb = charts._project_tsb(days, 84.9, 108.1)
    prim = [r["tsb"] for r in project_pmc_daily(84.9, 108.1, [113, 58, 55])]
    assert prim == chart_tsb


def test_project_pmc_zero_load_recovers_form():
    # With no training, ATL decays toward CTL faster than CTL falls → TSB rises.
    rows = project_pmc_daily(85.0, 100.0, [0, 0, 0, 0, 0])
    tsbs = [r["tsb"] for r in rows]
    assert tsbs == sorted(tsbs)            # monotonically improving
    assert tsbs[-1] > tsbs[0]


# ── per-session TSS reproduces logged actuals (rides dominate weekly load) ─────
def test_tss_calibration_against_actuals():
    def tss(sport, minutes, name):
        return pt._event_tss({"sport": sport, "minutes": minutes, "name": name})["tss"]
    assert abs(tss("Ride", 179, "Z2 ride") - 127) <= 3      # actual 127
    assert abs(tss("Run", 66, "Long Z2 run") - 63) <= 4     # actual 63


def test_tss_prefers_plan_load_target_over_estimate():
    # When the plan carries its own number, use it verbatim — never estimate over it.
    r = pt._event_tss({"sport": "Swim", "minutes": 60, "name": "CSS", "load_target": 90})
    assert r["tss"] == 90 and r["source"] == "plan"


# ── calculable TSS from time-at-intensity (the swim fix) ──────────────────────
def test_segments_tss_is_time_weighted_if_squared():
    # 30 min @ IF 1.0 + 30 min @ IF 0.6 → (0.5*1 + 0.5*0.36)*100 = 68
    r = tss_from_segments("bike", [{"minutes": 30, "if": 1.0}, {"minutes": 30, "if": 0.6}])
    assert r["tss"] == 68 and r["duration_min"] == 60


def test_segments_swim_css_beats_easy_same_duration():
    # The inversion we found (easy load_target >= CSS) must not survive the calc.
    css = tss_from_segments("swim", [{"minutes": 10, "zone": "easy"},
                                     {"minutes": 44, "zone": "css"},
                                     {"minutes": 6, "zone": "cooldown"}])
    easy = tss_from_segments("swim", [{"minutes": 8, "zone": "easy"},
                                      {"minutes": 28, "zone": "aerobic"},
                                      {"minutes": 4, "zone": "cooldown"}])
    assert css["tss"] > easy["tss"]
    assert 0.85 <= css["avg_if"] <= 0.95   # matches Jamie's logged ~0.90 swims


def test_segments_explicit_if_overrides_zone():
    r = tss_from_segments("run", [{"minutes": 60, "if": 0.8, "zone": "easy"}])
    assert r["segments"][0]["if"] == 0.8


# ── structured-workout rendering (Garmin sync) ────────────────────────────────
def test_render_flattens_repeats_no_header():
    # 8x(2m/1m) must expand to 16 lines (ICU's API collapses "Nx" headers, so we
    # flatten) — verified against a real push.
    r = render_workout("swim", [{"minutes": 10, "zone": "easy"},
                                {"repeat": 8, "steps": [{"minutes": 2, "zone": "css"},
                                                        {"minutes": 1, "zone": "recovery"}]},
                                {"minutes": 6, "zone": "cooldown"}])
    body = r["description"].splitlines()
    assert len(body) == 18                       # 1 WU + 8*2 + 1 CD
    assert not any(l.strip().endswith("x") or l.strip()[:2].isdigit() and "x" in l for l in body)
    assert all(l.startswith("- ") for l in body)
    assert r["duration_min"] == 40


def test_render_units_bike_power_vs_run_pace():
    bike = render_workout("bike", [{"minutes": 20, "zone": "sweetspot"}])["description"]
    run = render_workout("run", [{"minutes": 20, "zone": "threshold"}])["description"]
    assert "Pace" not in bike and "%" in bike      # bare % = power (%FTP)
    assert "Pace" in run                            # %pace for run/swim


# ── required-tss: real basis prescribes; no basis refuses (don't fabricate) ────
_JAMIE_CFG = {
    "plan_start": "2026-04-27",
    "phase_tss": {"base_end_week": 5, "build_end_week": 10,
                  "specific_end_week": 14, "peak_end_week": 17},
    "ctl_targets": {"race_min": 97,
                    "phase_ctl": {"base": 85, "build": 95, "specific": 105, "peak": 112}},
    "max_ctl_ramp_per_week": 4.0,
}


def test_required_tss_build_phase():
    out = pt.required_tss(_JAMIE_CFG, 82.9, today=date(2026, 6, 15))
    assert out["phase"] == "build"
    assert out["training_week"] == 8
    assert out["phase_target_ctl"] == 95
    # recommended is capped by the +4/wk ramp, never exceeds it
    assert out["recommended_weekly_tss"] == min(
        out["required_weekly_tss"], out["ramp_capped_weekly_tss"])


def test_required_tss_no_basis_refuses():
    out = pt.required_tss({"plan_start": "2026-04-27", "ctl_targets": {}}, 80.0,
                          today=date(2026, 6, 15))
    assert "error" in out and "recommended_weekly_tss" not in out


# ── weekly roll-up: actuals win, planned-remaining only, no double count ───────
def test_week_rollup_actual_beats_planned_same_day_sport():
    ws = date(2026, 6, 15)            # Monday
    today = date(2026, 6, 16)         # Tuesday
    history = [{"start_date_local": "2026-06-16T07:00", "type": "Run",
                "icu_training_load": 60}]
    events = [
        {"start_date_local": "2026-06-16T07:00", "type": "Run",   # already done → ignored
         "name": "Z2 run", "category": "WORKOUT", "load_target": 50},
        {"start_date_local": "2026-06-15T07:00", "type": "Ride",  # past day → ignored
         "name": "ride", "category": "WORKOUT", "load_target": 99},
        {"start_date_local": "2026-06-19T07:00", "type": "Ride",  # future → counted
         "name": "Z2 ride", "category": "WORKOUT", "load_target": 160},
    ]
    r = pt.week_rollup_summary(history, events, ws, today)
    assert r["completed_to_date_tss"] == 60
    assert r["planned_remaining_tss"] == 160        # only the future ride
    assert r["projected_week_tss"] == 220
