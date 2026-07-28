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
        # week 6 (build; not a cadence deload, nor the week after one) — last week
        # executed WAY under prescription (200 << the ~392 genuine-miss trigger =
        # 70% of the 7xCTL=560 maintenance load).
        r = pt.required_tss(_cfg(), 80.0, today=date(2026, 6, 1),
                            last_week_tss=200)
        assert r["week_type"] == "deload"
        assert "recovery week" in r["deload_reason"]

    def test_no_recovery_off_prior_deload(self):
        # week 5 immediately follows the week-4 scheduled deload; a badly-missed week
        # here must NOT re-fire recovery. A deload executed to its reduced prescription
        # reads as a 'miss' against the full target — that cascade is exactly what the
        # 'never fire recovery off a prior scheduled-deload week' guard kills.
        r = pt.required_tss(_cfg(), 80.0, today=date(2026, 5, 25),
                            last_week_tss=200)
        assert r["week_type"] != "deload"
        assert not r.get("deload_reason")

    def test_no_recovery_off_prior_taper(self):
        # week 14 (specific) follows a PLANNED B-race taper week (manual_easy_weeks,
        # Monday 2026-07-20). Jamie's Dorney taper executed ~511 TSS lands just under
        # 70% of maintenance (514 at CTL 105) — a planned down-week, NOT a miss, so it
        # must not fire a spurious recovery the next week.
        cfg = _cfg(manual_easy_weeks=[{"week_start": "2026-07-20",
                                       "reason": "B-race taper", "factor": 0.6}])
        # sanity: the prior week really is classified taper
        assert pt.required_tss(cfg, 105.0, today=date(2026, 7, 21))["week_type"] == "taper"
        r = pt.required_tss(cfg, 105.0, today=date(2026, 7, 28), last_week_tss=511)
        assert r["week_type"] == "specific"
        assert not r.get("deload_reason")

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


class TestBlockDeloadPlacement:
    """Placement is a BLOCK decision, not a counter (27 Jul 2026). The cadence
    proposes; block_deload_weeks decides. It may only MOVE a down-week, never
    delete one — recovery is not optional, its position is."""

    def _kathryn(self, **over):
        # Kathryn's real shape: plan_start 4 May, peak ends week 18, taper 7 Sep.
        # The every-4th cadence lands a deload on week 16 of 18 — only two loading
        # weeks between the unload and the taper, while she projects short of race_min.
        return _cfg(plan_start="2026-05-04", race_date="2026-09-20",
                    phase_tss={"base_end_week": 8, "build_end_week": 14,
                               "peak_end_week": 18},
                    ctl_targets={"race_min": 76, "race_max": 80},
                    max_ctl_ramp_per_week=6.0, **over)

    def test_deload_abutting_the_taper_moves_earlier(self):
        p = pt.block_deload_weeks(self._kathryn())
        assert p["cadence"] == [4, 8, 12, 16]
        assert sorted(p["weeks"]) == [4, 8, 12, 15]      # 16 -> 15
        assert p["moves"] == [{"from": 16, "to": 15}]
        assert p["unmoved_late"] == []

    def test_the_count_of_down_weeks_is_preserved(self):
        p = pt.block_deload_weeks(self._kathryn())
        # never trade recovery for CTL: as many deloads out as the cadence put in
        assert len(p["weeks"]) == len(p["cadence"])

    def test_required_tss_agrees_with_the_placement(self):
        cfg = self._kathryn()
        moved = pt.required_tss(cfg, 74.0, today=date(2026, 8, 10))    # week 15
        vacated = pt.required_tss(cfg, 74.0, today=date(2026, 8, 17))  # week 16
        assert moved["week_type"] == "deload"
        assert moved["deload_moved_from_week"] == 16
        assert moved["deload_reason"].startswith("scheduled deload")
        assert "moved earlier from cadence week 16" in moved["deload_reason"]
        assert vacated["week_type"] == "peak"
        assert "deload_reason" not in vacated

    def test_moving_a_deload_does_not_breach_the_ramp_cap(self):
        # the vacated week's target is still min(required, ramp-capped)
        r = pt.required_tss(self._kathryn(), 74.0, today=date(2026, 8, 17))
        assert r["recommended_weekly_tss"] <= r["ramp_capped_weekly_tss"]

    def test_an_early_cadence_deload_is_left_alone(self):
        # Jamie's shape: the week-16 deload has three loading weeks after it, so the
        # block has no complaint and placement must not fidget with it.
        cfg = _cfg(phase_tss={"base_end_week": 5, "build_end_week": 10,
                              "specific_end_week": 14, "peak_end_week": 19},
                   deload_skip_weeks=["2026-07-13"])
        p = pt.block_deload_weeks(cfg)
        assert p["cadence"] == [4, 8, 16]        # week 12 skipped by config
        assert sorted(p["weeks"]) == [4, 8, 16]
        assert p["moves"] == []

    def test_a_skip_week_is_never_chosen_as_the_destination(self):
        p = pt.block_deload_weeks(self._kathryn(deload_skip_weeks=["2026-08-10"]))
        assert 15 not in p["weeks"]              # week 15 = Mon 10 Aug, skipped
        assert p["moves"] == [{"from": 16, "to": 14}]

    def test_a_late_deload_beside_a_declared_easy_week_is_reported_not_deleted(self):
        # A manual easy week already unloads week 15, so the late cadence deload on
        # 16 cannot move earlier without landing on or beside a down-week. It is kept
        # and reported: dropping it may well be right here, but that is a per-athlete
        # judgement for deload_skip_weeks, not a rule that deletes recovery.
        p = pt.block_deload_weeks(self._kathryn(
            manual_easy_weeks=[{"week_start": "2026-08-10", "reason": "B-race",
                                "factor": 0.6}]))
        assert sorted(p["weeks"]) == [4, 8, 12, 16]
        assert p["unmoved_late"] == [16]
        assert p["moves"] == []

    def test_a_deload_is_never_dragged_beyond_one_cadence_period(self):
        # Over-constrained block: the only free earlier weeks are more than n weeks
        # back. Keep the deload where it is and say so, rather than oscillate.
        cfg = self._kathryn(deload_skip_weeks=["2026-08-10", "2026-08-03"])  # wk 15, 14
        p = pt.block_deload_weeks(cfg)
        assert sorted(p["weeks"]) == [4, 8, 12, 16]     # unchanged
        assert p["unmoved_late"] == [16]
        assert p["moves"] == []

    def test_an_unrepairable_block_keeps_its_deload_and_says_so(self):
        # A block with no room to move: every earlier week is also inside the window.
        p = pt.block_deload_weeks(_cfg(phase_tss={"base_end_week": 2,
                                                  "build_end_week": 4,
                                                  "peak_end_week": 4}))
        assert sorted(p["weeks"]) == [4]         # kept, not deleted
        assert p["unmoved_late"] == [4]
        assert p["moves"] == []

    def test_placement_does_not_depend_on_today(self):
        # Static from cfg: a week reads the same way in the Sunday build, the audit,
        # the projection and required_tss's own today-7 lookback.
        import inspect
        assert "today" not in inspect.signature(pt.block_deload_weeks).parameters
        cfg = self._kathryn()
        for day in (date(2026, 7, 28), date(2026, 8, 10), date(2026, 8, 17)):
            assert sorted(pt.block_deload_weeks(cfg)["weeks"]) == [4, 8, 12, 15], day

    def test_cadence_off_places_nothing(self):
        p = pt.block_deload_weeks(self._kathryn(deload_every_n_weeks=0))
        assert p["weeks"] == {} and p["cadence"] == []
