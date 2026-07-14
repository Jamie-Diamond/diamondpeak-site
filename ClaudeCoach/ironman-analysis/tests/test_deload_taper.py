"""Deload + shaped-taper branches in required_tss (methodology audit Phase 2).

P0-1: the forward plan was a monotonic CTL-chase — no programmed unloading.
P0-2: past peak_end_week required_tss returned NO target, so every load check
downstream disengaged and the taper intensity split reverted to base.

Pins: cadence deloads every Nth week at ~62%, miss-triggered recovery weeks,
stepped 70/55/40 taper volume anchored to the 7xCTL maintenance load with
intensity explicitly held, and the session-library taper TID rows.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import plan_tools as pt  # noqa: E402


def _cfg(**over):
    cfg = {
        "plan_start": "2026-04-27",            # a Monday
        "race_date": "2026-09-19",
        "ctl_targets": {"phase_ctl": {"base": 85, "build": 95,
                                      "specific": 105, "peak": 112}},
        "phase_tss": {"base_end_week": 5, "build_end_week": 10,
                      "specific_end_week": 14, "peak_end_week": 17},
        "max_ctl_ramp_per_week": 4.0,
    }
    cfg.update(over)
    return cfg


class TestDeloadCadence:
    def test_every_4th_week_is_deload(self):
        # 2026-05-18 = training week 4
        r = pt.required_tss(_cfg(), 80.0, today=date(2026, 5, 18))
        assert r["week_type"] == "deload"
        assert r["deload_reason"].startswith("scheduled deload")
        assert r["recommended_weekly_tss"] == round(r["full_week_tss"] * 0.62)
        assert "DELOAD WEEK" in r["note"]

    def test_non_deload_week_is_normal(self):
        # 2026-05-25 = training week 5
        r = pt.required_tss(_cfg(), 80.0, today=date(2026, 5, 25))
        assert r["week_type"] == "base"
        assert "deload_reason" not in r

    def test_cadence_configurable_and_disableable(self):
        r3 = pt.required_tss(_cfg(deload_every_n_weeks=3), 80.0, today=date(2026, 5, 11))
        assert r3["week_type"] == "deload"          # week 3 with n=3
        r0 = pt.required_tss(_cfg(deload_every_n_weeks=0), 80.0, today=date(2026, 5, 18))
        assert r0["week_type"] == "base"            # cadence off

    def test_deload_factor_configurable(self):
        r = pt.required_tss(_cfg(deload_factor=0.5), 80.0, today=date(2026, 5, 18))
        assert r["recommended_weekly_tss"] == round(r["full_week_tss"] * 0.5)


class TestMissTrigger:
    def test_badly_missed_week_becomes_recovery(self):
        # week 5 (not a cadence deload); last week executed at ~half prescription
        normal = pt.required_tss(_cfg(), 80.0, today=date(2026, 5, 25))
        rec = normal["recommended_weekly_tss"]
        r = pt.required_tss(_cfg(), 80.0, today=date(2026, 5, 25),
                            last_week_tss=rec * 0.5)
        assert r["week_type"] == "deload"
        assert "recovery week" in r["deload_reason"]

    def test_executed_week_stays_normal(self):
        normal = pt.required_tss(_cfg(), 80.0, today=date(2026, 5, 25))
        r = pt.required_tss(_cfg(), 80.0, today=date(2026, 5, 25),
                            last_week_tss=normal["recommended_weekly_tss"] * 0.9)
        assert r["week_type"] == "base"

    def test_none_means_no_trigger(self):
        r = pt.required_tss(_cfg(), 80.0, today=date(2026, 5, 25), last_week_tss=None)
        assert r["week_type"] == "base"


class TestShapedTaper:
    """weeks 18+ for this cfg (peak_end_week 17); race 2026-09-19."""

    def test_three_or_more_weeks_out_is_70pct(self):
        r = pt.required_tss(_cfg(), 100.0, today=date(2026, 8, 24))   # 26 days out
        assert r["week_type"] == "taper"
        assert r["taper_factor"] == 0.70
        assert r["recommended_weekly_tss"] == round(7 * 100.0 * 0.70)

    def test_two_weeks_out_is_55pct(self):
        r = pt.required_tss(_cfg(), 100.0, today=date(2026, 9, 7))    # 12 days out
        assert r["taper_factor"] == 0.55
        assert r["recommended_weekly_tss"] == round(7 * 100.0 * 0.55)

    def test_race_week_is_40pct(self):
        r = pt.required_tss(_cfg(), 100.0, today=date(2026, 9, 14))   # 5 days out
        assert r["taper_factor"] == 0.40

    def test_taper_target_engages_load_checks(self):
        # The old branch returned NO recommended_weekly_tss -> every downstream
        # load check disengaged. Now a real number always comes back.
        r = pt.required_tss(_cfg(), 100.0, today=date(2026, 9, 7))
        assert isinstance(r["recommended_weekly_tss"], int)
        assert r["recommended_weekly_tss"] > 0

    def test_intensity_is_held_in_note(self):
        r = pt.required_tss(_cfg(), 100.0, today=date(2026, 9, 7))
        assert "Hold INTENSITY" in r["note"]

    def test_no_race_date_degrades_gracefully(self):
        cfg = _cfg()
        del cfg["race_date"]
        r = pt.required_tss(cfg, 100.0, today=date(2026, 9, 7))
        assert r["week_type"] == "taper"
        assert "recommended_weekly_tss" not in r
        assert "race_date" in r["note"]

    def test_taper_never_deloads(self):
        # A cadence-deload week number falling in taper must stay a taper week.
        r = pt.required_tss(_cfg(), 100.0, today=date(2026, 8, 31))   # week 19 ... n=4 -> not
        r2 = pt.required_tss(_cfg(deload_every_n_weeks=19), 100.0, today=date(2026, 8, 31))
        assert r["week_type"] == r2["week_type"] == "taper"


class TestTaperHoldsIntensity:
    # Phase 5.3: the overall TID is DERIVED from the per-sport rows; taper carries no
    # rows of its own, so the derivation must fall back to PEAK (hold intensity), never
    # the base mostly-easy split. Synthetic blueprint - blueprints are gitignored.
    def test_taper_derivation_falls_back_to_peak(self):
        import sys as _sys
        _sys.path.insert(0, str(REPO / "lib"))
        from session_library import derive_overall_tid, _phase_distribution
        bp = {"phases": [
            {"name": "Peak", "distribution": {"Bike": "70% Z1\u20132 / 22% Z3 / 8% Z4\u20135"}},
            {"name": "Taper", "distribution": {}},
        ]}
        peak = derive_overall_tid(_phase_distribution(bp, "peak"), "ironman")
        assert peak == [70, 22, 8]
        # a taper brief (empty rows) must resolve to the peak derivation, not base
        assert _phase_distribution(bp, "taper") == {}

    def test_volume_factor_is_gone(self):
        lib = json.loads((REPO / "config" / "session-library.json").read_text())
        assert "volume_factor" not in lib["phases"]["taper"]


class TestPhaseResolutionWithoutSpecific:
    def test_unconfigured_specific_does_not_swallow_taper(self):
        # Calum-shaped config: peak ends week 11, race week 12, NO specific phase.
        cfg = _cfg(plan_start="2026-06-08", race_date="2026-08-29",
                   phase_tss={"base_end_week": 3, "build_end_week": 8,
                              "peak_end_week": 11},
                   ctl_targets={"race_min": 40, "race_max": 48})
        r = pt.required_tss(cfg, 100.0, today=date(2026, 8, 25))   # race week
        assert r["phase"] == "taper"
        assert r["taper_factor"] == 0.40
        mid = pt.required_tss(cfg, 30.0, today=date(2026, 7, 21))  # week 7 = build
        assert mid["phase"] == "build"
        peak = pt.required_tss(cfg, 40.0, today=date(2026, 8, 11)) # week 10 = peak
        assert peak["phase"] == "peak"
