"""Tests for the mid-plan fitness check in scripts/generate-blueprint.py.

The check must compare current CTL against the entry range of the phase
containing TODAY, not the plan's first phase (a mid-plan regen used to demand
a coaching decision for CTL 80 vs Base 55–70 nine weeks into the plan).
"""
from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
GB = REPO / "scripts" / "generate-blueprint.py"


@pytest.fixture(scope="module")
def gb():
    spec = importlib.util.spec_from_file_location("generate_blueprint", GB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _phases(today):
    """Base finished 3 weeks ago; Build contains today; Taper later."""
    return [
        {"name": "Base",  "start": (today - timedelta(weeks=9)).isoformat(),
         "end": (today - timedelta(weeks=3, days=1)).isoformat()},
        {"name": "Build", "start": (today - timedelta(weeks=3)).isoformat(),
         "end": (today + timedelta(weeks=3)).isoformat()},
        {"name": "Taper", "start": (today + timedelta(weeks=3, days=1)).isoformat(),
         "end": (today + timedelta(weeks=5)).isoformat()},
    ]


class TestFitnessCheck:
    def test_mid_plan_checks_current_phase_not_first(self, gb):
        # CTL 80 is over Base (55–70) but inside Build (70–85): no decision needed
        assert gb.fitness_check("x", "Full Ironman", 80.0, _phases(date.today()), None) is None

    def test_mid_plan_overfit_for_current_phase_still_fires(self, gb):
        # 1.10 × Build high (85) = 93.5 — CTL 95 exceeds it
        note = gb.fitness_check("x", "Full Ironman", 95.0, _phases(date.today()), None)
        assert note and "AWAITING_DECISION" in note and "Build" in note

    def test_before_plan_start_uses_first_phase(self, gb):
        future = [{"name": p["name"],
                   "start": (date.fromisoformat(p["start"]) + timedelta(weeks=12)).isoformat(),
                   "end": (date.fromisoformat(p["end"]) + timedelta(weeks=12)).isoformat()}
                  for p in _phases(date.today())]
        note = gb.fitness_check("x", "Full Ironman", 80.0, future, None)
        assert note and "Base" in note  # original pre-plan behaviour preserved

    def test_in_taper_never_flags_overfitness(self, gb):
        today = date.today()
        taper_now = [{"name": "Taper", "start": (today - timedelta(days=3)).isoformat(),
                      "end": (today + timedelta(weeks=2)).isoformat()}]
        assert gb.fitness_check("x", "Full Ironman", 120.0, taper_now, None) is None


class TestSpecificPhaseContent:
    """Specific carries its own content rows since 2026-06-10 (Jamie sign-off)."""

    def test_content_family_no_longer_maps_to_build(self, gb):
        assert gb.content_family("specific") == "specific"

    def test_if_target_between_build_and_peak(self, gb):
        assert gb.IF_TARGETS["specific"] == 0.70
        assert gb.tss_ceiling(15, "Specific") == 735.0   # 15h x 100 x 0.70^2

    def test_ctl_entry_range(self, gb):
        assert gb.ctl_range("Full Ironman", "Specific") == (75, 90)

    def test_distribution_row_exists(self, gb):
        # Interpolated, not canonical: blueprint.md section 3.2 states Base/Build/Peak
        # and (since 11 Sep 2026) Taper, but never Specific. Previously pinned to
        # "72%"/"78%", the pre-doc hand-written literals.
        D = gb.DISTRIBUTION["Full Ironman"]
        d, build, peak = D["specific"], D["build"], D["peak"]
        _easy = lambda row: float(__import__("re").search(r"(\d+)", row).group(1))
        for sport in ("Bike", "Run", "Swim"):
            assert _easy(peak[sport]) <= _easy(d[sport]) <= _easy(build[sport]), sport
            assert sum(float(x) for x in __import__("re").findall(
                r"(\d+(?:\.\d+)?)\s*%", d[sport])) == 100, sport

    def test_fuelling_is_race_rate_on_all_key_sessions(self, gb):
        note = gb.fuelling_note("Full Ironman", "Specific")
        assert "race rate" in note and "ALL key sessions" in note

    def test_event_without_specific_row_falls_back_gracefully(self, gb):
        # 70.3 has no specific content rows — lookups must not blow up
        assert gb.ctl_range("70.3", "Specific") is None
        assert gb.fuelling_note("70.3", "Specific") == "Follow phase-progressive protocol."


class TestTaperDistribution:
    """The Taper row, added to blueprint.md section 3.2 on 11 Sep 2026.

    Before it existed every generated blueprint carried `distribution: {}` for its
    Taper phase, and validate_week turns that into a HARD `taper_distribution_missing`
    — so the check written specifically to catch "an all-race-pace taper week read as
    zero quality" was unarmed in the only week it applies to, for every athlete. Jamie
    and Kathryn were both red on it eight days before Cervia.
    """

    def test_no_event_can_regress_to_an_empty_taper(self, gb):
        """The actual regression lock: an event with a peak row must have a taper row.

        Asserted over EVERY event the parser produces rather than the two spelled out
        in section 3.2, because Sportive's numbers come from a different place (the
        section 4 prose) and a taper added to one table and not the other is exactly
        how this comes back for one athlete only.
        """
        for event, phases in gb.DISTRIBUTION.items():
            assert phases.get("taper"), event
            assert set(phases["taper"]) == set(phases["peak"]), event

    def test_taper_carries_peaks_proportions(self, gb):
        """Section 3.2's stated rule: taper cuts VOLUME, not the mix.

        Pinned to peak rather than to literal percentages so the two move together —
        a taper floor left behind after peak is revised is a floor nobody chose.
        """
        import re
        for event in ("Full Ironman", "70.3", "Sportive"):
            taper, peak = gb.DISTRIBUTION[event]["taper"], gb.DISTRIBUTION[event]["peak"]
            assert taper == peak, event
            for sport, row in taper.items():
                pct = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", row)]
                assert sum(pct) == 100, (event, sport, row)

    def test_sportive_taper_is_read_from_the_doc_not_copied(self, gb, tmp_path,
                                                            monkeypatch):
        """Fails CLOSED when the doc drops the Taper step.

        Sportive's row is parsed out of the section 4 prose, so the tempting shortcut
        is `out["Sportive"]["taper"] = peak` in the generator. That is the drift
        _parse_distribution_doc exists to prevent: it would keep passing while the doc
        said something else. Removing the step from the doc must RAISE.
        """
        src = (REPO / "blueprints" / "blueprint.md").read_text()
        assert "Peak 65/18/17 -> Taper 65/18/17" in src.replace("\u2192", "->")
        (tmp_path / "blueprints").mkdir()
        (tmp_path / "blueprints" / "blueprint.md").write_text(
            src.replace(" \u2192 Taper 65/18/17", ""))
        monkeypatch.setattr(gb, "BASE", tmp_path)
        with pytest.raises(AssertionError, match="Sportive bike distribution"):
            gb._parse_distribution_doc()

    def test_a_taper_phase_emits_a_non_empty_distribution(self, gb):
        """End to end: the sidecar the audit actually reads."""
        today = date.today()
        # date objects, not strings: build_blueprint_data calls .isoformat() on these
        # (generate-blueprint's internal shape, per primitives.blueprint's docstring).
        phases = [
            {"name": "Peak", "start": today - timedelta(weeks=2),
             "end": today - timedelta(days=1), "weeks": 2},
            {"name": "Taper", "start": today,
             "end": today + timedelta(weeks=2), "weeks": 2},
        ]
        profile = {"name": "Test", "race_distance": "Full Ironman",
                   "max_hours_per_week": 12,
                   "race_date": (today + timedelta(weeks=2)).isoformat()}
        data = gb.build_blueprint_data("test", profile, phases, 80.0, None)
        taper = next(p for p in data["phases"] if p["name"] == "Taper")
        assert taper["distribution"] == gb.DISTRIBUTION["Full Ironman"]["taper"]

    def test_that_distribution_disarms_taper_distribution_missing(self, gb):
        """The point of the whole change, asserted against the real validator.

        A Taper week with a distribution must not raise taper_distribution_missing;
        the same week with the old `{}` still must, so this cannot pass by the rule
        having been weakened somewhere else.
        """
        import sys
        sys.path.insert(0, str(REPO / "ironman-analysis"))
        from primitives import validate_plan as vp

        ws = date.today() - timedelta(days=date.today().weekday())
        week = [{"type": "Ride", "start_date_local": (ws + timedelta(days=d)).isoformat(),
                 "moving_time": 3600} for d in (0, 2, 4)]
        codes = lambda dist: [v.code for v in vp.validate_week(
            week, ws, phase_family="taper", distribution=dist).violations]
        assert "taper_distribution_missing" not in codes(
            gb.DISTRIBUTION["Full Ironman"]["taper"])
        assert "taper_distribution_missing" in codes({})
