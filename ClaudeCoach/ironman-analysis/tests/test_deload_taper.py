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


def _frozen_date(y, m, d):
    """A `date` SUBCLASS whose today() is fixed, for testing the completed-window guard.

    Returns the CLASS, not an instance: plan_tools calls `date.today()`, and patching in
    an instance leaves today() bound to the real calendar - which made two of these tests
    pass only because the real date happened to be 2026-08-10.
    """
    class _D(date):
        @classmethod
        def today(cls):
            return date(y, m, d)
    return _D


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


class TestReturnToLoadAndDefactoDeload:
    """Both from Jamie and Kathryn, 10 Aug 2026.

    _MISS_TRIGGER only fires on a COLLAPSE (< 70% of maintenance). The commoner case
    is a week that merely failed to BUILD, and nothing caught it: Kathryn ran 474
    against 584 planned with maintenance at 489 - 97% of maintenance, so no branch
    tripped - and the next week was then sized purely off the CTL line at 690, which
    is +46% on what she had actually done and above her best realised ramp all season.
    On top of that the cadence scheduled a deload, stacking two unchosen down-weeks
    into the first week of her Peak phase while she sat 6 CTL below her race band.
    """

    def _kathryn(self, **over):
        return _cfg(plan_start="2026-05-04", race_date="2026-09-20",
                    phase_tss={"base_end_week": 8, "build_end_week": 14,
                               "peak_end_week": 18},
                    ctl_targets={"race_min": 76, "race_max": 80},
                    max_ctl_ramp_per_week=6.0, **over)

    # Kathryn's real numbers: CTL 69.9 -> maintenance 489, last week executed 474.
    K_CTL, K_LAST, K_MAINT = 69.9, 474.0, 489

    def test_step_up_is_capped_after_a_week_at_or_below_maintenance(self):
        # Week 17 (24 Aug) is an ordinary loading week, so only the cap is in play.
        r = pt.required_tss(self._kathryn(), self.K_CTL,
                            today=date(2026, 8, 24), last_week_tss=self.K_LAST)
        assert r["week_type"] != "deload"
        assert r["return_step_cap"] == round(self.K_LAST * 1.30)
        assert r["recommended_weekly_tss"] == r["return_step_cap"]
        assert r["uncapped_weekly_tss"] > r["return_step_cap"]
        assert "RETURN TO LOAD" in r["note"]

    def test_cap_never_prescribes_detraining(self):
        # 100 x 1.30 is far below maintenance; the cap floors at maintenance so it
        # cannot ask for less load than holding fitness requires.
        r = pt.required_tss(self._kathryn(), self.K_CTL,
                            today=date(2026, 8, 24), last_week_tss=100.0)
        assert r["return_step_cap"] == self.K_MAINT

    def test_no_cap_after_a_genuine_build_week(self):
        r = pt.required_tss(self._kathryn(), self.K_CTL,
                            today=date(2026, 8, 24), last_week_tss=628.0)
        assert "return_step_cap" not in r

    def test_no_cap_when_last_week_is_unknown(self):
        # A fetch failure must never silently cap the prescription.
        r = pt.required_tss(self._kathryn(), self.K_CTL,
                            today=date(2026, 8, 24), last_week_tss=None)
        assert "return_step_cap" not in r

    def test_a_planned_deload_does_not_ratchet_the_return_week_down(self):
        # THE regression this guard exists for. Week 12 (20 Jul) is a scheduled
        # deload; the week after it must return to FULL load. Without the guard the
        # cap fired off the deload's own reduced load and dragged 636 down to 515,
        # which defeats the point of unloading and ratchets down after every deload.
        r = pt.required_tss(self._kathryn(), 67.2,
                            today=date(2026, 7, 27), last_week_tss=396.0)
        assert "return_step_cap" not in r
        assert r["recommended_weekly_tss"] == r["required_weekly_tss"]
        assert "deload_may_be_redundant" not in r

    def test_scheduled_deload_after_a_flat_week_is_questioned_not_skipped(self):
        # Week 15 (10 Aug) is where block_deload_weeks places her deload.
        r = pt.required_tss(self._kathryn(), self.K_CTL,
                            today=date(2026, 8, 10), last_week_tss=self.K_LAST)
        # The deload STANDS: recovery is never silently stripped.
        assert r["week_type"] == "deload"
        q = r["deload_may_be_redundant"]
        assert q["last_week_tss"] == int(self.K_LAST)
        assert q["maintenance_weekly_tss"] == self.K_MAINT
        assert "de facto deload" in q["question"]

    def test_an_earned_deload_is_not_questioned(self):
        r = pt.required_tss(self._kathryn(), self.K_CTL,
                            today=date(2026, 8, 10), last_week_tss=640.0)
        assert r["week_type"] == "deload"
        assert "deload_may_be_redundant" not in r


class TestLookbackWindowMustBeOver:
    """The miss-trigger's lookback must not read a week that has not finished.

    Found 10 Aug 2026 by dry-running the generator off-cadence. Building w/c 17 Aug on
    Monday 10 Aug made the lookback 10-16 Aug - the week IN PROGRESS, 33 TSS logged so
    far. That is under 70% of the 489 maintenance, so _MISS_TRIGGER fired and turned a
    PEAK week into a 303 TSS recovery week (489 x 0.62), for an athlete whose deload had
    been deliberately removed. Pre-existing, and it silently HALVES the target of any
    week rebuilt early - which is exactly what someone does when they want next week's
    plan in advance.
    """

    class _Client:
        def __init__(self): self.calls = 0
        def get_training_history(self, days=0, sport=None):
            self.calls += 1
            return [{"start_date_local": "2026-08-04T09:00:00", "icu_training_load": 216},
                    {"start_date_local": "2026-08-06T09:00:00", "icu_training_load": 258},
                    {"start_date_local": "2026-08-10T09:00:00", "icu_training_load": 33}]

    def test_a_finished_week_is_summed(self, monkeypatch):
        # Window 3-9 Aug, entirely in the past relative to 10 Aug.
        monkeypatch.setattr(pt, "date", _frozen_date(2026, 8, 10))
        c = self._Client()
        assert pt.last_week_actual_tss(c, today=date(2026, 8, 10)) == 474.0
        assert c.calls == 1

    def test_the_sunday_cron_window_ending_TODAY_is_allowed(self, monkeypatch):
        # The 18:00 Sunday build targets the next Monday, so its window ends on that
        # same Sunday. Rejecting hi == today would disable the trigger on the one
        # cadence that actually uses it.
        monkeypatch.setattr(pt, "date", _frozen_date(2026, 8, 9))
        c = self._Client()
        assert pt.last_week_actual_tss(c, today=date(2026, 8, 10)) == 474.0
        assert c.calls == 1

    def test_a_window_reaching_into_the_future_is_refused(self, monkeypatch):
        monkeypatch.setattr(pt, "date", _frozen_date(2026, 8, 10))
        c = self._Client()
        # Building w/c 17 Aug early: window 10-16 Aug is not over.
        assert pt.last_week_actual_tss(c, today=date(2026, 8, 17)) is None
        assert c.calls == 0, "must refuse before spending an API call"

    def test_the_future_week_is_no_longer_spuriously_deloaded(self):
        cfg = _cfg(plan_start="2026-05-04", race_date="2026-09-20",
                   deload_skip_weeks=["2026-08-17"],
                   ctl_targets={"race_min": 76, "race_max": 80},
                   phase_tss={"base_end_week": 8, "build_end_week": 14,
                              "peak_end_week": 18},
                   max_ctl_ramp_per_week=6.0)
        bad = pt.required_tss(cfg, 69.9, today=date(2026, 8, 17), last_week_tss=33.0)
        good = pt.required_tss(cfg, 69.9, today=date(2026, 8, 17), last_week_tss=None)
        assert bad["week_type"] == "deload"          # what the partial week produced
        assert good["week_type"] != "deload"         # what "unknown" correctly produces
        assert good["recommended_weekly_tss"] > bad["recommended_weekly_tss"] * 2
