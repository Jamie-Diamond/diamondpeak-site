"""Macro (block-level) feasibility projection — lib/macro_projection.py.

The Sunday generator builds six days at a time, so nothing above it ever asked
whether the weeks that remain can actually reach the CTL target inside the ramp
cap and the hours ceiling. These pin the two failure shapes the live system was
blind to:

  * RAMP/CTL shortfall — too much CTL to find in too few weeks (the projection
    arrives below ctl_targets.race_min).
  * CEILING infeasibility — the phase CTL target demands weekly load above the
    phase TSS ceiling, so the weekly generator can only overshoot the load cap or
    silently miss the target. This is the shape a pure shortfall test misses: an
    athlete already at race_min can still be overshooting his ceiling every week.

Also pinned: the projection introduces no arithmetic of its own (it iterates
plan_tools.required_tss and primitives.load.compute_projected_ctl), it is pure,
and its ceiling tolerance is READ OFF validate_week rather than restated.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))

import macro_projection as mp                        # noqa: E402
import plan_tools as pt                              # noqa: E402
from primitives.load import compute_projected_ctl     # noqa: E402
from primitives.validate_plan import validate_week    # noqa: E402


def _cfg(**over):
    cfg = {
        "name": "Test",
        "plan_start": "2026-04-27",
        "race_date": "2026-09-19",
        "phase_tss": {"base_end_week": 5, "build_end_week": 10,
                      "specific_end_week": 14, "peak_end_week": 19},
        "ctl_targets": {"race_min": 97,
                        "phase_ctl": {"base": 85, "build": 95,
                                      "specific": 105, "peak": 112}},
        "max_ctl_ramp_per_week": 4.0,
        "deload_every_n_weeks": 0,      # off unless a test asks for it
    }
    cfg.update(over)
    return cfg


def _bp(ceiling_peak=778, ceiling_spec=735):
    return {"phases": [
        {"name": "Specific", "family": "specific", "start": "2026-07-06",
         "end": "2026-08-02", "weeks": 4, "tss_ceiling": ceiling_spec},
        {"name": "Peak", "family": "peak", "start": "2026-08-03",
         "end": "2026-09-06", "weeks": 5, "tss_ceiling": ceiling_peak},
        {"name": "Taper", "family": "taper", "start": "2026-09-07",
         "end": "2026-09-19", "weeks": 2, "tss_ceiling": None},
    ]}


def _codes(rep):
    return {f["code"] for f in rep["flags"]}


# ── the two failure shapes ────────────────────────────────────────────────────
def test_ceiling_infeasible_when_phase_target_exceeds_hours_ceiling():
    """An athlete AT race_min can still be structurally overshooting: reaching the
    peak CTL target needs ~865 TSS/wk against a 778 ceiling. A weekly-only planner
    cannot see this; the macro projection must."""
    rep = mp.project_block(_cfg(), _bp(), ctl_now=96.6, today=date(2026, 8, 3))
    assert "ceiling_infeasible" in _codes(rep)
    assert rep["hard_flag"] is True
    bad = [w for w in rep["weeks"] if w["ceiling_infeasible"]]
    assert bad, "expected at least one week over its ceiling"
    assert all(w["engine_target_tss"] > w["phase_tss_ceiling"] for w in bad)
    # and the buildable load is CAPPED, so the projection does not pretend the
    # generator will deliver a week the validator would reject.
    assert all(w["buildable_tss"] <= round(w["phase_tss_ceiling"] * 1.1) for w in bad)


def test_ceiling_infeasible_block_reports_the_strict_ceiling_trajectory():
    """On a ceiling-infeasible block the headline CTL is only reached by building
    every week ABOVE cap (weeks the audit hard-fails). The projection must also
    report where the block lands with the tolerance UNSPENT, or the coach reads
    'CTL fine' and 'over ceiling' as contradictory."""
    rep = mp.project_block(_cfg(), _bp(), ctl_now=96.6, today=date(2026, 8, 3))
    assert rep["ctl_at_race_week_start_at_ceiling"] < rep["ctl_at_race_week_start"]
    assert rep["ctl_at_taper_start_at_ceiling"] < rep["ctl_at_taper_start"]
    detail = next(f["detail"] for f in rep["flags"] if f["code"] == "ceiling_infeasible")
    assert str(rep["ctl_at_race_week_start_at_ceiling"]) in detail
    # per-week: the strict figure never exceeds the tolerated one
    assert all(w["buildable_at_ceiling_tss"] <= w["buildable_tss"] for w in rep["weeks"])


def test_ceiling_infeasible_clears_when_the_ceiling_is_raised():
    """Same block, more hours available — the flag must disappear. Proves the flag
    tracks the ceiling and is not an artefact of the phase target alone."""
    rep = mp.project_block(_cfg(), _bp(ceiling_peak=1100), ctl_now=96.6,
                           today=date(2026, 8, 3))
    assert "ceiling_infeasible" not in _codes(rep)


def test_ctl_shortfall_when_a_cadence_deload_eats_a_loading_week():
    """Low CTL a few weeks out, with the mechanical every-4th-week deload landing on
    one of the last loading weeks: the projection must arrive short of race_min and
    say by how much. Paired with the no-deload run below, which just reaches it —
    i.e. the DELOAD PLACEMENT, a decision nothing above the week currently makes, is
    what decides whether the block is feasible at all. That is the whole argument for
    a macro layer."""
    cfg = _cfg(deload_every_n_weeks=4,
               plan_start="2026-06-08", race_date="2026-08-29",
               phase_tss={"base_end_week": 3, "build_end_week": 8, "peak_end_week": 11},
               ctl_targets={"race_min": 40, "race_max": 48},
               max_ctl_ramp_per_week=5.0)
    bp = {"phases": [
        {"name": "Build", "family": "build", "start": "2026-06-29",
         "end": "2026-08-02", "weeks": 5, "tss_ceiling": 370},
        {"name": "Peak", "family": "peak", "start": "2026-08-03",
         "end": "2026-08-23", "weeks": 3, "tss_ceiling": 415},
        {"name": "Taper", "family": "taper", "start": "2026-08-24",
         "end": "2026-08-29", "weeks": 1, "tss_ceiling": None},
    ]}
    rep = mp.project_block(cfg, bp, ctl_now=26.4, today=date(2026, 7, 27))
    assert "ctl_shortfall" in _codes(rep)
    assert rep["hard_flag"] is True
    assert rep["ctl_at_race_week_start"] < 40
    detail = next(f["detail"] for f in rep["flags"] if f["code"] == "ctl_shortfall")
    assert "below ctl_targets.race_min 40" in detail
    # the deload IS the difference: same block, cadence off, and it just arrives.
    cfg_no_deload = dict(cfg, deload_every_n_weeks=0)
    rep2 = mp.project_block(cfg_no_deload, bp, ctl_now=26.4, today=date(2026, 7, 27))
    assert "ctl_shortfall" not in _codes(rep2)
    assert rep2["ctl_at_race_week_start"] > rep["ctl_at_race_week_start"]


def test_no_shortfall_flag_when_the_block_reaches_the_target():
    cfg = _cfg(ctl_targets={"race_min": 60, "phase_ctl": {"base": 85, "build": 95,
                                                          "specific": 105, "peak": 112}})
    rep = mp.project_block(cfg, _bp(), ctl_now=96.6, today=date(2026, 8, 3))
    assert "ctl_shortfall" not in _codes(rep)


# ── one engine, one projection: no fifth source of truth ─────────────────────
def test_week_targets_are_exactly_required_tss():
    """Every week's target must be byte-identical to what required_tss returns for
    that week at that projected CTL — the macro layer must not compute its own."""
    today = date(2026, 8, 3)
    rep = mp.project_block(_cfg(), _bp(), ctl_now=96.6, today=today)
    ctl = 96.6
    for w in rep["weeks"]:
        r = pt.required_tss(_cfg(), round(ctl, 1),
                            today=date.fromisoformat(w["week_start"]),
                            last_week_tss=None)
        assert w["engine_target_tss"] == int(r["recommended_weekly_tss"])
        assert w["week_type"] == r["week_type"]
        ctl = compute_projected_ctl(ctl, w["buildable_tss"], 1)
        assert w["ctl_end"] == round(ctl, 1)


def test_cap_tolerance_is_read_off_validate_week():
    """If validate_week's tolerance moves, the macro projection moves with it."""
    import inspect
    expected = inspect.signature(validate_week).parameters["tss_tolerance"].default
    assert mp._cap_tolerance() == float(expected)
    rep = mp.project_block(_cfg(), _bp(), ctl_now=96.6, today=date(2026, 8, 3))
    assert rep["cap_tolerance"] == float(expected)


def test_pure_and_injectable_no_io():
    """project_block must run with a stub engine and a stub ceiling resolver, i.e.
    it reads no config file, no calendar and no network of its own."""
    calls = []

    def fake_required(cfg, ctl, today=None, last_week_tss=None):
        calls.append((ctl, today, last_week_tss))
        return {"phase": "peak", "week_type": "peak", "required_weekly_tss": 500,
                "ramp_capped_weekly_tss": 500, "recommended_weekly_tss": 500,
                "phase_target_ctl": 100}

    rep = mp.project_block(_cfg(), _bp(), ctl_now=80.0, today=date(2026, 9, 1),
                           required_fn=fake_required,
                           ceiling_for=lambda ws, phase: 600.0,
                           last_week_tss=123.0)
    assert [w["engine_target_tss"] for w in rep["weeks"]] == [500, 500, 500]
    # last_week_tss is applied to the FIRST week only (a forward projection cannot
    # know about a future miss, and required_tss's recovery branch is bounded on it).
    assert [c[2] for c in calls] == [123.0, None, None]


def test_heat_overlay_flagged_on_near_ceiling_weeks():
    rep = mp.project_block(_cfg(), _bp(), ctl_now=96.6, today=date(2026, 8, 3),
                           heat_start=date(2026, 8, 22))
    hot = next((f for f in rep["flags"] if f["code"] == "heat_overlay"), None)
    assert hot is not None
    assert all(wk >= "2026-08-17" for wk in hot["weeks"])
    # and no flag at all when there is no heat block
    rep2 = mp.project_block(_cfg(), _bp(), ctl_now=96.6, today=date(2026, 8, 3))
    assert "heat_overlay" not in _codes(rep2)


def test_deload_placement_reported_from_the_cadence():
    """A cadence deload is placement information the block never sees today."""
    rep = mp.project_block(_cfg(deload_every_n_weeks=4), _bp(), ctl_now=96.6,
                           today=date(2026, 8, 3))
    dl = next(f for f in rep["flags"] if f["code"] == "deload_placement")
    assert dl["weeks"], "expected at least one down-week"
    assert any(w["week_type"] == "deload" for w in rep["weeks"])


def test_no_slack_when_every_loading_week_is_ramp_pinned():
    """A CTL gap far bigger than the ramp can close: every loading week runs at the
    cap, so one missed week is unrecoverable. Ceiling deliberately generous so this
    tests the ramp, not the hours."""
    rep = mp.project_block(_cfg(), _bp(ceiling_peak=5000, ceiling_spec=5000),
                           ctl_now=60.0, today=date(2026, 8, 3))
    loading = [w for w in rep["weeks"] if w["week_type"] not in ("taper", "deload")]
    assert loading and all(w["ramp_limited"] for w in loading)
    assert "no_slack" in _codes(rep)
    # and NOT flagged when there is real headroom under the cap
    rep2 = mp.project_block(_cfg(ctl_targets={"race_min": 97, "phase_ctl":
                                              {"base": 85, "build": 95,
                                               "specific": 105, "peak": 98}}),
                            _bp(ceiling_peak=5000), ctl_now=96.6,
                            today=date(2026, 8, 3))
    assert "no_slack" not in _codes(rep2)


def test_macro_plan_absence_is_reported():
    rep = mp.project_block(_cfg(), _bp(), ctl_now=96.6, today=date(2026, 8, 3))
    assert "no_macro_plan" in _codes(rep)
    rep2 = mp.project_block(_cfg(), _bp(), ctl_now=96.6, today=date(2026, 8, 3),
                            has_macro_plan=True)
    assert "no_macro_plan" not in _codes(rep2)


def test_errors_are_explicit_not_silent():
    assert "error" in mp.project_block(_cfg(race_date=None), _bp(), 96.6,
                                       today=date(2026, 8, 3))
    assert "error" in mp.project_block(_cfg(), _bp(), 0, today=date(2026, 8, 3))
    # a week the engine cannot target must abort, never be given an invented load
    out = mp.project_block(_cfg(plan_start=None), _bp(), 96.6, today=date(2026, 8, 3))
    assert "error" in out


def test_late_loading_window_is_shared_with_plan_tools():
    """If the placement rule's window moves, the flag that reports on it moves too."""
    assert mp.LATE_LOADING_WINDOW == pt.LATE_LOADING_WINDOW


def test_block_placement_moves_a_late_deload_and_closes_the_shortfall(monkeypatch):
    """Kathryn, 28 Jul 2026: the every-4th-week cadence put a deload on week 16 of 18
    and the block arrived 1.6 CTL short of race_min 76. Block-aware placement moves
    that same deload to week 15 — same number of down-weeks, one week earlier — and
    the block reaches the target. Counterfactual: window 0 restores pure cadence."""
    cfg = _cfg(name="Kathryn", plan_start="2026-05-04", race_date="2026-09-20",
               phase_tss={"base_end_week": 8, "build_end_week": 14,
                          "peak_end_week": 18},
               ctl_targets={"race_min": 76, "race_max": 80},
               max_ctl_ramp_per_week=6.0, deload_every_n_weeks=4)
    bp = {"phases": [
        {"name": "Build", "family": "build", "start": "2026-06-29",
         "end": "2026-08-02", "weeks": 5, "tss_ceiling": 966},
        {"name": "Peak", "family": "peak", "start": "2026-08-03",
         "end": "2026-09-06", "weeks": 5, "tss_ceiling": 1083},
        {"name": "Taper", "family": "taper", "start": "2026-09-07",
         "end": "2026-09-20", "weeks": 2, "tss_ceiling": None},
    ]}
    after = mp.project_block(cfg, bp, ctl_now=67.7, today=date(2026, 7, 28))
    monkeypatch.setattr(pt, "LATE_LOADING_WINDOW", 0)      # pure cadence again
    before = mp.project_block(cfg, bp, ctl_now=67.7, today=date(2026, 7, 28))

    def deloads(rep):
        return [w["week_start"] for w in rep["weeks"] if w["week_type"] == "deload"]

    assert deloads(before) == ["2026-08-17"]
    assert deloads(after) == ["2026-08-10"]
    assert len(deloads(after)) == len(deloads(before))     # moved, not removed
    assert "ctl_shortfall" in _codes(before)
    assert "ctl_shortfall" not in _codes(after)
    assert after["ctl_at_race_week_start"] > before["ctl_at_race_week_start"]
    assert after["ctl_at_race_week_start"] >= 76
    # and no week was pushed past the athlete's ramp cap to get there
    assert not any(w["ramp_limited"] for w in after["weeks"])


# ── which constraint binds: hours ceiling vs CTL-ramp cap ─────────────────────
# The defect these pin is not that the hours ceiling exists — hours are a real
# constraint and letting the ramp cap override them would prescribe sessions the
# athlete cannot do. The defect was that a ceiling clipping the athlete BELOW what
# their own ramp cap already deems safe was invisible, and indistinguishable from
# never having been checked.

def test_binding_constraint_reports_hours_when_the_ceiling_is_the_lower_bound():
    b = mp.binding_constraint(778, 856)
    assert b["binding"] == "hours"
    assert b["gap_tss"] == 78            # how far below the safe maximum he is clipped


def test_binding_constraint_reports_ramp_when_the_ramp_cap_is_the_lower_bound():
    assert mp.binding_constraint(1083, 744)["binding"] == "ramp"


def test_binding_constraint_ties_go_to_ramp_not_a_zero_gap_finding():
    # Equal bounds are not a conflict; calling them hours-bound would raise a
    # coach-facing finding with nothing to act on.
    assert mp.binding_constraint(500, 500)["binding"] == "ramp"


def test_binding_constraint_refuses_to_conclude_when_a_bound_is_missing():
    # Taper weeks: no source carries a ceiling AND required_tss's taper branch
    # returns no ramp_capped_weekly_tss. Neither verdict may be invented.
    for c, r in ((None, 856), (778, None), (None, None), (0, 856)):
        out = mp.binding_constraint(c, r)
        assert out["binding"] == "unbounded", (c, r)
        assert out["gap_tss"] is None


def test_hours_bound_below_ramp_is_flagged_and_names_both_readings():
    # Ceiling well under the ramp-permitted maximum -> the hours figure limits him.
    rep = mp.project_block(_cfg(), _bp(ceiling_peak=778, ceiling_spec=735), 96.6,
                           today=date(2026, 7, 27))
    assert "hours_bound_below_ramp" in _codes(rep)
    assert rep["binding_constraint"] == "hours"
    detail = next(f for f in rep["flags"]
                  if f["code"] == "hours_bound_below_ramp")["detail"]
    # BOTH options must be named — the module must not choose for the coach.
    assert "ctl_targets" in detail and "STALE" in detail
    assert "max_hours_per_week" in detail


def test_ramp_bound_block_says_so_rather_than_staying_silent():
    # Ceiling raised far above the ramp-permitted maximum -> ramp binds, which is
    # the design intent. Silence here is what made "checked and fine" and "not
    # checked" look identical.
    rep = mp.project_block(_cfg(), _bp(ceiling_peak=3000, ceiling_spec=3000), 96.6,
                           today=date(2026, 7, 27))
    assert "ramp_bound" in _codes(rep)
    assert "hours_bound_below_ramp" not in _codes(rep)
    assert rep["binding_constraint"] == "ramp"


def test_neither_binding_verdict_ever_contributes_to_hard_flag():
    # hard_flag drives the CLI exit code, so neither new finding may flip an
    # athlete's exit status: "working as designed" must not page anyone, and the
    # hours conflict is already carried by ceiling_infeasible where it is fatal.
    for bp in (_bp(ceiling_peak=778, ceiling_spec=735),        # hours-bound
               _bp(ceiling_peak=3000, ceiling_spec=3000)):     # ramp-bound
        rep = mp.project_block(_cfg(), bp, 96.6, today=date(2026, 7, 27))
        binding = {"hours_bound_below_ramp", "ramp_bound", "binding_unknown"}
        assert {f["code"] for f in rep["flags"]} & binding, "no binding verdict emitted"
        for f in rep["flags"]:
            if f["code"] in binding:
                assert f["severity"] != "hard", f
        # Removing the binding verdicts must leave hard_flag exactly as it was.
        assert rep["hard_flag"] == any(f["severity"] == "hard" for f in rep["flags"]
                                       if f["code"] not in binding)


def test_ramp_permitted_is_the_engines_own_figure_not_a_second_formula():
    # The anti-drift coupling: the ramp-permitted maximum reported per week must be
    # required_tss's OWN ramp_capped_weekly_tss (the exact 42-day EMA solve in
    # primitives.load.compute_required_tss), not a linearised restatement such as
    # 7 x (CTL + 6R), which runs ~2% low and would flip a marginal verdict.
    cfg, bp = _cfg(), _bp()
    rep = mp.project_block(cfg, bp, 96.6, today=date(2026, 7, 27))
    wk = rep["weeks"][0]
    expected = pt.required_tss(cfg, 96.6, today=date(2026, 7, 27),
                              last_week_tss=None)["ramp_capped_weekly_tss"]
    assert wk["ramp_permitted_tss"] == expected
    assert wk["ramp_permitted_tss"] != int(7 * (96.6 + 6 * 4.0))


def test_plan_audit_imports_the_comparison_rather_than_restating_it():
    # ONE place. Three bugs in this repo came from duplicated load maths; the audit
    # must call the same function, not its own copy.
    import plan_audit
    assert plan_audit.binding_constraint is mp.binding_constraint
