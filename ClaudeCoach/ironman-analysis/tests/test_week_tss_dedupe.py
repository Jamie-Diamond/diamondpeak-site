"""Week-TSS must not count a completed session AND its still-planned twin.

Kathryn, logged 2026-07-29: week of 27 Jul returned total_tss 729 when the correct
de-duplicated total is 612, which is what the injected DETERMINISTIC PLANNING block
already said. The 117 difference is exactly two planned sessions that were summed on
top of the completed activity occupying the same slot:

    Mon 27 Jul   completed Cardio      31   +  planned Kettlebell  20
    Tue 28 Jul   completed VirtualRide 88   +  planned Sweetspot   97  (type Ride)

    20 + 97 = 117;  729 - 117 = 612

Cause: the de-duplication compared RAW ICU type strings, so "VirtualRide" never
matched its own planned "Ride" twin, and "Cardio" never matched "Kettlebell". Jamie's
standing rule already said to treat Gravel and Virtual Ride as subsets of Ride; it had
never been implemented in code on this path, even though seven other ad-hoc
sport-family maps existed elsewhere in the tree.

Inflating a week's TSS by ~19% is not cosmetic: it feeds the shortfall/ramp reasoning,
so the coach reports a week as on-target when it is under, or demands load that is
already there.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))

from plan_tools import _already_completed, _sport_family  # noqa: E402


class TestSportFamily:
    @pytest.mark.parametrize("icu_type,family", [
        ("Ride", "bike"),
        ("VirtualRide", "bike"),
        ("GravelRide", "bike"),
        ("MountainBikeRide", "bike"),
        ("Run", "run"),
        ("TrailRun", "run"),
        ("Swim", "swim"),
        ("OpenWaterSwim", "swim"),
        ("Kettlebell", "strength"),
        ("Cardio", "strength"),
        ("WeightTraining", "strength"),
    ])
    def test_known_types_collapse(self, icu_type, family):
        assert _sport_family(icu_type) == family

    def test_unknown_type_falls_back_to_itself(self):
        """No silent widening: an unmapped type de-duplicates exactly as before."""
        assert _sport_family("Brick") == "brick"
        assert _sport_family("Kitesurf") == "kitesurf"

    def test_blank_and_none_do_not_raise(self):
        assert _sport_family("") == "?"
        assert _sport_family(None) == "?"


class TestKathrynWeek27Jul:
    """The reported case, reproduced from the bug report's own figures."""

    MON_COMPLETED = [{"sport": "Cardio", "tss": 31, "status": "completed"}]
    TUE_COMPLETED = [{"sport": "VirtualRide", "tss": 88, "status": "completed"}]

    def test_planned_kettlebell_is_absorbed_by_completed_cardio(self):
        assert _already_completed("Kettlebell", self.MON_COMPLETED) is True

    def test_planned_ride_is_absorbed_by_completed_virtualride(self):
        assert _already_completed("Ride", self.TUE_COMPLETED) is True

    def test_week_total_is_612_not_729(self):
        """The arithmetic the bug report gives, driven through the real predicate."""
        days = {
            "2026-07-27": (self.MON_COMPLETED, [("Kettlebell", 20)]),
            "2026-07-28": (self.TUE_COMPLETED, [("Ride", 97)]),
        }
        # Remaining days of that week summed to 493 completed+planned, untouched here.
        other_days_tss = 493
        total = other_days_tss
        for _d, (completed, planned) in days.items():
            total += sum(c["tss"] for c in completed)
            for ptype, ptss in planned:
                if not _already_completed(ptype, completed):
                    total += ptss
        assert total == 612, f"expected the de-duplicated 612, got {total}"

        # And prove the old raw-string comparison is what produced 729.
        legacy = other_days_tss
        for _d, (completed, planned) in days.items():
            legacy += sum(c["tss"] for c in completed)
            for ptype, ptss in planned:
                if not any(c["sport"] == ptype for c in completed):
                    legacy += ptss
        assert legacy == 729, "the pre-fix comparison should reproduce the reported 729"


class TestNoOverDeduplication:
    """The fix must not swallow sessions that are genuinely separate."""

    def test_two_different_families_both_stand(self):
        completed = [{"sport": "Swim", "tss": 40}]
        assert _already_completed("Run", completed) is False
        assert _already_completed("Ride", completed) is False

    def test_brick_is_not_absorbed_by_a_completed_ride(self):
        """Brick is its own slot - a completed Ride must not cancel a planned Brick."""
        assert _already_completed("Brick", [{"sport": "Ride", "tss": 90}]) is False

    def test_empty_completed_day_absorbs_nothing(self):
        assert _already_completed("Ride", []) is False
        assert _already_completed("Ride", None) is False

    def test_same_sport_twice_still_dedupes_as_before(self):
        """Unchanged behaviour: a completed Run already absorbed a planned Run."""
        assert _already_completed("Run", [{"sport": "Run", "tss": 60}]) is True
