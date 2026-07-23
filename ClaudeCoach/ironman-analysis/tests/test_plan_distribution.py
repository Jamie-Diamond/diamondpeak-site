"""Tests for lib/plan_distribution.py — the two-directional intensity gate.

Pinned by the 22 Jul failure: the bot improvised Kathryn's forward week from
prose rules, zeroing out her Build-phase Run Z4–5 slice (blueprint requires
78% Z1–2 / 12% Z3 / 10% Z4–5) while asserting it was spec-compliant. The
generator-side check only caught EXCESS quality; a missing quality slice reached
the athlete uncaught. This gate must flag insufficiency as well as excess, and
the phase label for 27 Jul must resolve to Build from config (not memory).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # repo root
sys.path.insert(0, str(REPO / "lib"))
sys.path.insert(0, str(REPO / "ironman-analysis"))

import plan_distribution as pd                          # noqa: E402
from primitives.blueprint import resolve_phases, current_phase  # noqa: E402

# Kathryn's Build-phase run distribution (athletes/kathryn/reference/
# training-blueprint.json, Build phase).
KATHRYN_BUILD = {
    "Swim": "65% Z1–2 / 0% Z3 / 35% Z4–5",
    "Bike": "70% Z1–2 / 18% Z3 / 12% Z4–5",
    "Run":  "78% Z1–2 / 12% Z3 / 10% Z4–5",
}


def _run(easy, z3, z45):
    """A run week with the given minutes in each band, as one session per band."""
    segs = []
    if easy:
        segs.append({"minutes": easy, "zone": "z2"})
    if z3:
        segs.append({"minutes": z3, "zone": "tempo"})
    if z45:
        segs.append({"minutes": z45, "zone": "threshold"})
    return [{"sport": "Run", "name": "run week", "segments": segs}]


class TestParseDistribution:
    def test_parses_endash_row(self):
        assert pd.parse_distribution("78% Z1–2 / 12% Z3 / 10% Z4–5") == {
            "easy": 78.0, "z3": 12.0, "z45": 10.0}

    def test_missing_z3_defaults_zero(self):
        assert pd.parse_distribution("70% Z1–2 / 30% Z4–5") == {
            "easy": 70.0, "z3": 0.0, "z45": 30.0}

    def test_blank_row_is_none(self):
        assert pd.parse_distribution("") is None
        assert pd.parse_distribution(None) is None


class TestCompliantWeekPasses:
    def test_kathryn_build_compliant_run_ok(self):
        # 234/36/30 of 300 min = 78/12/10 exactly.
        findings = pd.audit_distribution(KATHRYN_BUILD, _run(234, 36, 30))
        assert len(findings) == 1
        f = findings[0]
        assert f.sport == "Run"
        assert f.ok, f.violations
        assert not pd.any_offspec(findings)

    def test_z45_at_spec_floor_passes(self):
        # Exactly at target Z4–5 = 10%: must NOT flag (test (a) — Z4–5 ≥ floor).
        findings = pd.audit_distribution(KATHRYN_BUILD, _run(234, 36, 30))
        assert findings[0].actual["z45"] >= 10.0
        assert findings[0].ok


class TestMissingSliceFlagged:
    def test_no_z45_is_flagged(self):
        # The 22 Jul shape: all easy + a little Z3, ZERO Z4–5.
        findings = pd.audit_distribution(KATHRYN_BUILD, _run(264, 36, 0))
        assert pd.any_offspec(findings)
        v = " ".join(findings[0].violations)
        assert "Z4–5" in v and "no" in v.lower()

    def test_all_easy_flags_both_slices(self):
        findings = pd.audit_distribution(KATHRYN_BUILD, _run(300, 0, 0))
        v = " ".join(findings[0].violations)
        assert "Z4–5" in v and "Z3" in v


class TestExcessQualityFlagged:
    def test_too_much_quality_flagged(self):
        # 50% easy vs 78% target — excess intensity.
        findings = pd.audit_distribution(KATHRYN_BUILD, _run(150, 0, 150))
        assert pd.any_offspec(findings)
        assert any("too much quality" in x for x in findings[0].violations)


class TestScopeAndGuards:
    def test_swim_and_brick_ignored(self):
        sessions = [
            {"sport": "Swim", "segments": [{"minutes": 60, "zone": "css"}]},
            {"sport": "Brick", "segments": [{"minutes": 120, "zone": "race"}]},
        ]
        assert pd.audit_distribution(KATHRYN_BUILD, sessions) == []

    def test_tiny_volume_not_judged(self):
        # Under MIN_MINUTES: no finding even if lopsided.
        findings = pd.audit_distribution(KATHRYN_BUILD, _run(60, 0, 0))
        assert findings == []

    def test_unknown_zone_tracked_not_bucketed(self):
        sessions = [{"sport": "Run", "segments": [
            {"minutes": 200, "zone": "z2"}, {"minutes": 100, "zone": "mystery"}]}]
        f = pd.audit_distribution(KATHRYN_BUILD, sessions)[0]
        assert f.unknown_min == 100.0


class TestPhaseLabelFromConfig:
    """Test (c): 27 Jul 2026 resolves to Build from config, never narrated Peak."""

    def _phases(self):
        # Kathryn's athletes.json config, inline for hermeticity.
        return resolve_phases(
            plan_start=date(2026, 5, 4),
            phase_tss={"base_end_week": 8, "build_end_week": 14, "peak_end_week": 18},
            race_date=date(2026, 9, 20),
            today=date(2026, 7, 27),
        )

    def test_27_jul_is_build(self):
        phases = [{**p, "start": p["start"].isoformat(), "end": p["end"].isoformat()}
                  for p in self._phases()]
        ph = current_phase({"phases": phases}, date(2026, 7, 27))
        assert ph is not None
        assert ph["name"] == "Build", f"27 Jul resolved to {ph['name']}, expected Build"

    def test_27_jul_not_peak(self):
        phases = [{**p, "start": p["start"].isoformat(), "end": p["end"].isoformat()}
                  for p in self._phases()]
        ph = current_phase({"phases": phases}, date(2026, 7, 27))
        assert ph["family"] != "peak"
