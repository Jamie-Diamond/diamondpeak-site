"""Tests for primitives/modulation.py."""
from __future__ import annotations

import pytest

from primitives.modulation import (
    modulate_session,
    SessionPrescription,
    _ATL_SWAP_GAP,
    _ATL_MODERATE_GAP,
    _HRV_TREND_HARD,
    _RPE_REDUCTION_THRESHOLD,
    _SLEEP_SWAP_H,
    _SLEEP_REDUCE_H,
    _INTENSITY_STEP,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def planned_threshold(overrides=None):
    base = {
        "session_type": "bike_threshold",
        "target_intensity": 1.0,
        "interval_count": 4,
        "interval_duration_min": 10.0,
        "recovery_min": 3.0,
        "total_duration_min": 75,
    }
    if overrides:
        base.update(overrides)
    return base


def planned_run_quality(overrides=None):
    base = {
        "session_type": "run_quality",
        "target_intensity": 1.0,
        "interval_count": 6,
        "interval_duration_min": 5.0,
        "recovery_min": 1.5,
        "total_duration_min": 60,
    }
    if overrides:
        base.update(overrides)
    return base


def planned_z2(overrides=None):
    base = {
        "session_type": "bike_z2",
        "target_intensity": 0.65,
        "interval_count": None,
        "interval_duration_min": None,
        "recovery_min": None,
        "total_duration_min": 120,
    }
    if overrides:
        base.update(overrides)
    return base


def fresh_readiness(overrides=None):
    """Optimal readiness — no rules should fire."""
    base = {
        "atl": 110,
        "ctl": 105,
        "hrv_trend_pct": +2.0,
        "sleep_h_last_night": 8.0,
        "last_session_rpe": 6,
        "ankle_pain_score": 0,
        "ankle_quality_cleared": True,
        "temp_c": 15.0,
        "dew_point_c": 10.0,
    }
    if overrides:
        base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# No-modification case
# ---------------------------------------------------------------------------

class TestNoModification:
    def test_fresh_athlete_no_rules_fire(self):
        p = modulate_session(planned_threshold(), fresh_readiness())
        assert not p.modified
        assert p.go
        assert not p.swapped_to_z2
        assert p.applied_rules == []
        assert p.target_intensity == 1.0
        assert p.interval_count == 4

    def test_z2_session_immune_to_soft_rules(self):
        # High ATL but Z2 session — no quality to modulate
        r = fresh_readiness({"atl": 150, "ctl": 120, "hrv_trend_pct": -10.0})
        p = modulate_session(planned_z2(), r)
        assert not p.modified
        assert p.go


# ---------------------------------------------------------------------------
# R1 — Ankle hard stop
# ---------------------------------------------------------------------------

class TestR1Ankle:
    def test_pain_above_cap_blocks_run_quality(self):
        r = fresh_readiness({"ankle_pain_score": 3})
        p = modulate_session(planned_run_quality(), r)
        assert not p.go
        assert "R1" in p.applied_rules
        assert "3/10" in p.reasoning_trails[0]

    def test_pain_at_cap_does_not_block(self):
        r = fresh_readiness({"ankle_pain_score": 2})
        p = modulate_session(planned_run_quality(), r)
        assert p.go
        assert "R1" not in p.applied_rules

    def test_ankle_not_cleared_blocks_run_quality(self):
        r = fresh_readiness({"ankle_pain_score": 0, "ankle_quality_cleared": False})
        p = modulate_session(planned_run_quality(), r)
        assert not p.go
        assert "R1" in p.applied_rules

    def test_ankle_not_cleared_does_not_block_bike(self):
        r = fresh_readiness({"ankle_pain_score": 0, "ankle_quality_cleared": False})
        p = modulate_session(planned_threshold(), r)
        assert p.go
        assert "R1" not in p.applied_rules

    def test_r1_blocks_brick(self):
        planned = {
            "session_type": "brick",
            "target_intensity": 0.71,
            "interval_count": None,
            "total_duration_min": 120,
        }
        r = fresh_readiness({"ankle_pain_score": 4})
        p = modulate_session(planned, r)
        assert not p.go
        assert "R1" in p.applied_rules


# ---------------------------------------------------------------------------
# R2 — ATL swap
# ---------------------------------------------------------------------------

class TestR2AtkSwap:
    def test_atl_above_threshold_swaps_bike_to_z2(self):
        r = fresh_readiness({"atl": 150, "ctl": 120})  # gap = 30 > 25
        p = modulate_session(planned_threshold(), r)
        assert p.swapped_to_z2
        assert p.session_type == "bike_z2"
        assert "R2" in p.applied_rules
        assert p.go

    def test_atl_at_threshold_does_not_swap(self):
        r = fresh_readiness({"atl": 145, "ctl": 120})  # gap = 25, not > 25
        p = modulate_session(planned_threshold(), r)
        assert not p.swapped_to_z2

    def test_atl_above_threshold_swaps_run_to_easy(self):
        r = fresh_readiness({"atl": 150, "ctl": 120})
        p = modulate_session(planned_run_quality(), r)
        assert p.swapped_to_z2
        assert p.session_type == "run_easy"

    def test_swap_preserves_duration(self):
        r = fresh_readiness({"atl": 152, "ctl": 120})
        p = modulate_session(planned_threshold(), r)
        assert p.total_duration_min == planned_threshold()["total_duration_min"]


# ---------------------------------------------------------------------------
# R3 — HRV intensity drop
# ---------------------------------------------------------------------------

class TestR3Hrv:
    def test_hrv_below_threshold_reduces_intensity_and_intervals(self):
        r = fresh_readiness({"hrv_trend_pct": -9.0})
        p = modulate_session(planned_threshold(), r)
        assert "R3" in p.applied_rules
        assert p.target_intensity == pytest.approx(1.0 - _INTENSITY_STEP, abs=1e-4)
        assert p.interval_count == 3  # 4 - 1

    def test_hrv_at_threshold_does_not_fire(self):
        r = fresh_readiness({"hrv_trend_pct": _HRV_TREND_HARD})  # exactly -7%, not below
        p = modulate_session(planned_threshold(), r)
        assert "R3" not in p.applied_rules

    def test_hrv_trail_cites_value(self):
        r = fresh_readiness({"hrv_trend_pct": -8.5})
        p = modulate_session(planned_threshold(), r)
        trail = next(t for t in p.reasoning_trails if "R3" in t or "HRV" in t)
        assert "-8.5" in trail


# ---------------------------------------------------------------------------
# R4 — ATL moderate cap
# ---------------------------------------------------------------------------

class TestR4AtkModerate:
    def test_moderate_atl_caps_intensity(self):
        r = fresh_readiness({"atl": 138, "ctl": 120})  # gap = 18, in 15–25 range
        p = modulate_session(planned_threshold(), r)
        assert "R4" in p.applied_rules
        assert p.target_intensity <= 0.95

    def test_r4_does_not_fire_below_lower_bound(self):
        r = fresh_readiness({"atl": 134, "ctl": 120})  # gap = 14, below 15
        p = modulate_session(planned_threshold(), r)
        assert "R4" not in p.applied_rules

    def test_r4_does_not_fire_above_upper_bound(self):
        r = fresh_readiness({"atl": 148, "ctl": 120})  # gap = 28 > 25, R2 fires instead
        p = modulate_session(planned_threshold(), r)
        assert "R4" not in p.applied_rules
        assert "R2" in p.applied_rules


# ---------------------------------------------------------------------------
# R5 — Prior RPE
# ---------------------------------------------------------------------------

class TestR5PriorRpe:
    def test_high_prior_rpe_reduces_intensity(self):
        r = fresh_readiness({"last_session_rpe": 8})
        p = modulate_session(planned_threshold(), r)
        assert "R5" in p.applied_rules
        assert p.target_intensity < 1.0

    def test_rpe_below_threshold_no_fire(self):
        r = fresh_readiness({"last_session_rpe": 7})
        p = modulate_session(planned_threshold(), r)
        assert "R5" not in p.applied_rules

    def test_null_rpe_no_fire(self):
        r = fresh_readiness({"last_session_rpe": None})
        p = modulate_session(planned_threshold(), r)
        assert "R5" not in p.applied_rules


# ---------------------------------------------------------------------------
# R6 — Sleep two-signal swap
# ---------------------------------------------------------------------------

class TestR6Sleep:
    def test_two_signal_swaps(self):
        r = fresh_readiness({"sleep_h_last_night": 5.5, "hrv_trend_pct": -6.0})
        p = modulate_session(planned_threshold(), r)
        assert p.swapped_to_z2
        assert "R6" in p.applied_rules

    def test_single_poor_sleep_no_swap(self):
        r = fresh_readiness({"sleep_h_last_night": 5.5, "hrv_trend_pct": +1.0})
        p = modulate_session(planned_threshold(), r)
        assert not p.swapped_to_z2

    def test_single_poor_sleep_reduces_intervals(self):
        # Sleep 6.5h (between 6 and 7) with fine HRV → reduce intervals only
        r = fresh_readiness({"sleep_h_last_night": 6.5, "hrv_trend_pct": +1.0})
        p = modulate_session(planned_threshold(), r)
        assert not p.swapped_to_z2
        assert "R6-single" in p.applied_rules
        assert p.interval_count == 3  # 4 - 1

    def test_good_sleep_no_fire(self):
        r = fresh_readiness({"sleep_h_last_night": 7.5})
        p = modulate_session(planned_threshold(), r)
        assert "R6" not in p.applied_rules
        assert "R6-single" not in p.applied_rules


# ---------------------------------------------------------------------------
# R7 — Heat
# ---------------------------------------------------------------------------

class TestR7Heat:
    def test_cool_conditions_no_adjustment(self):
        r = fresh_readiness({"temp_c": 18.0, "dew_point_c": 10.0})
        p = modulate_session(planned_threshold(), r)
        assert "R7" not in p.applied_rules

    def test_hot_conditions_reduce_intensity(self):
        r = fresh_readiness({"temp_c": 28.0, "dew_point_c": 18.0})
        p = modulate_session(planned_threshold(), r)
        assert "R7" in p.applied_rules
        assert p.target_intensity < 1.0

    def test_heat_applies_to_run_quality(self):
        r = fresh_readiness({"temp_c": 28.0, "dew_point_c": 18.0})
        p = modulate_session(planned_run_quality(), r)
        assert "R7" in p.applied_rules


# ---------------------------------------------------------------------------
# Stacking — multiple rules fire together
# ---------------------------------------------------------------------------

class TestStacking:
    def test_hrv_and_heat_stack(self):
        """R3 (HRV) and R7 (heat) both fire — intensity should be doubly reduced."""
        r = fresh_readiness({
            "hrv_trend_pct": -9.0,   # R3
            "temp_c": 28.0,
            "dew_point_c": 18.0,     # R7
        })
        p_fresh = modulate_session(planned_threshold(), fresh_readiness())
        p_stacked = modulate_session(planned_threshold(), r)
        assert "R3" in p_stacked.applied_rules
        assert "R7" in p_stacked.applied_rules
        assert p_stacked.target_intensity < p_fresh.target_intensity

    def test_r1_blocks_all_other_rules(self):
        """When R1 fires, no other rules should appear (early return)."""
        r = fresh_readiness({
            "ankle_pain_score": 5,
            "hrv_trend_pct": -10.0,
            "atl": 155, "ctl": 120,
        })
        p = modulate_session(planned_run_quality(), r)
        assert p.applied_rules == ["R1"]
        assert not p.go

    def test_r2_blocks_soft_rules(self):
        """When R2 fires (ATL swap), soft rules do not further reduce."""
        r = fresh_readiness({
            "atl": 152, "ctl": 120,  # R2
            "hrv_trend_pct": -10.0,  # would be R3
        })
        p = modulate_session(planned_threshold(), r)
        assert "R2" in p.applied_rules
        assert "R3" not in p.applied_rules

    def test_intensity_never_below_floor(self):
        """Intensity should not drop below 0.65 regardless of how many rules stack."""
        r = fresh_readiness({
            "hrv_trend_pct": -15.0,
            "last_session_rpe": 10,
            "temp_c": 35.0,
            "dew_point_c": 22.0,
            "sleep_h_last_night": 6.8,
        })
        p = modulate_session(planned_threshold(), r)
        assert p.target_intensity >= 0.65


# ---------------------------------------------------------------------------
# Summary string sanity
# ---------------------------------------------------------------------------

class TestSummary:
    def test_no_mod_summary(self):
        p = modulate_session(planned_threshold(), fresh_readiness())
        assert "No adjustments" in p.summary

    def test_swap_summary_mentions_rule(self):
        r = fresh_readiness({"atl": 152, "ctl": 120})
        p = modulate_session(planned_threshold(), r)
        assert "R2" in p.summary

    def test_modified_summary_mentions_changes(self):
        r = fresh_readiness({"hrv_trend_pct": -9.0})
        p = modulate_session(planned_threshold(), r)
        assert "R3" in p.summary
