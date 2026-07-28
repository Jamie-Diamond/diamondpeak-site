"""Tests for lib/rule_conflicts.py — the [perm]-rule contradiction detector.

Each test pins one of the contradiction shapes observed live on 28 Jul 2026, restated with
synthetic rules and events so the suite needs no athlete data:
  A a prose rule names a training day the athlete's day_rules does not permit (or the
    athlete has no day_rules at all, so nothing can enforce the rule);
  B a planned session of the anchored KIND lands on a day the prose rules out;
  C that session also advertises an intensity the prose reserves against (advisory);
  D a withholding rule the coach REVIEWED AND ACCEPTED is served anyway in the same week;
  E a dated [expires:] exception and a [perm] rule are both live and disagree.
Plus the two precision properties without which the detector is unusable: an anchor binds a
session KIND rather than a whole sport, and a weekday inside a provenance trail
(\"confirmed 27 Jul 2026 ... Sunday ...\") is history, not a rule.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))

import rule_conflicts as rcf                                  # noqa: E402

MON = date(2026, 7, 27)                                       # a Monday


def _ev(day_offset: int, sport: str, name: str, desc: str = ""):
    return {"category": "WORKOUT", "type": sport, "name": name, "description": desc,
            "start_date_local": (MON.fromordinal(MON.toordinal() + day_offset)).isoformat()
                                 + "T06:00:00"}


def _rules(tmp_path: Path, slug: str, *lines: str) -> Path:
    d = tmp_path / "athletes" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "persistent-rules.md").write_text("".join(l + "\n" for l in lines))
    return tmp_path


LONG_RIDE_FRI = ("[perm] Friday anchor = a long Z2 ride (>=210 min); the primary aerobic "
                 "diagnostic. Friday long ride is pure Z2 by default: no sweetspot or "
                 "threshold blocks without an explicit request.")
LONG_RUN_WED = ("[perm] Long run = WEDNESDAY anchor (locked; non-negotiable); never omit or "
                "substitute with a non-run session.")


class TestAxisAConfig:
    def test_prose_day_missing_from_day_rules(self, tmp_path):
        base = _rules(tmp_path, "a", "[perm] Fixed swim days are Tue and Wed.")
        out = rcf.check_athlete("a", base, cfg={"day_rules": {"swim_days": ["Tue", "Thu"]}})
        assert [f["code"] for f in out] == ["config_gap"]
        assert "Wed" in out[0]["detail"] and "swim_days" in out[0]["detail"]

    def test_no_day_rules_at_all_is_hard(self, tmp_path):
        base = _rules(tmp_path, "a", "[perm] All cycling on weekdays only - no weekend rides.")
        out = rcf.check_athlete("a", base, cfg={})
        assert [(f["code"], f["severity"]) for f in out] == [("config_absent", "hard")]

    def test_agreeing_prose_and_config_are_silent(self, tmp_path):
        base = _rules(tmp_path, "a", LONG_RUN_WED)
        assert rcf.check_athlete("a", base, cfg={"day_rules": {"run_days": ["Wed", "Sat"]}}) == []


class TestAxisBCalendar:
    def test_anchored_session_on_the_wrong_day(self, tmp_path):
        base = _rules(tmp_path, "a", LONG_RUN_WED)
        out = rcf.check_athlete("a", base, events=[_ev(1, "Run", "Long run")])
        assert [f["code"] for f in out] == ["run_prose_day_breach"]
        assert "2026-07-28" in out[0]["detail"]

    def test_an_anchor_binds_the_session_kind_not_the_whole_sport(self, tmp_path):
        """'The long run is Wednesday' does not forbid a Saturday easy run. Without this
        scoping one anchor reports a breach against every other session of that sport and
        buries the real finding."""
        base = _rules(tmp_path, "a", LONG_RUN_WED)
        assert rcf.check_athlete("a", base, events=[_ev(5, "Run", "Easy run 45min")]) == []

    def test_a_qualifier_bound_to_another_sport_does_not_match(self, tmp_path):
        """'Brick run off long ride' is not the long RUN."""
        base = _rules(tmp_path, "a", LONG_RUN_WED)
        assert rcf.check_athlete("a", base,
                                 events=[_ev(3, "Run", "Brick run off long ride")]) == []

    def test_a_weekday_in_a_provenance_trail_is_not_a_claim(self, tmp_path):
        base = _rules(tmp_path, "a",
                      "[perm] Protect sleep over a droppable easy session - agreed 17 Jul "
                      "2026 when the run was skipped to protect Sunday's quality bike.")
        assert rcf.check_athlete("a", base, cfg={"day_rules": {"bike_days": ["Tue"]}},
                                 events=[_ev(1, "Ride", "Sweetspot 3x20")]) == []

    def test_virtual_and_gravel_rides_count_as_bike(self, tmp_path):
        base = _rules(tmp_path, "a", "[perm] All cycling on weekdays only - no weekend rides.")
        out = rcf.check_athlete("a", base, events=[_ev(6, "GravelRide", "Sunday gravel")])
        assert [f["code"] for f in out] == ["bike_prose_day_breach"]


class TestAxisCIntensity:
    def test_reserved_intensity_breach_is_advisory(self, tmp_path):
        base = _rules(tmp_path, "a", LONG_RIDE_FRI)
        out = rcf.check_athlete("a", base,
                                events=[_ev(4, "Ride", "Long ride - race-IF finish 2x45")])
        assert [(f["axis"], f["severity"]) for f in out] == [("C", "soft")]
        assert out[0]["cue"]                                    # the matched cue is shown

    def test_a_compliant_session_is_silent(self, tmp_path):
        base = _rules(tmp_path, "a", LONG_RIDE_FRI)
        assert rcf.check_athlete("a", base,
                                 events=[_ev(4, "Ride", "Long ride", "Pure Z2, NP 55-75%")]) == []


class TestAxisEExpiry:
    def test_dated_exception_contradicting_a_perm_rule(self, tmp_path):
        base = _rules(tmp_path, "a",
                      "[perm] All cycling on weekdays only - no weekend rides.",
                      "[expires:2099-01-01] Saturday long rides permitted for the build.")
        out = rcf.check_athlete("a", base)
        assert [f["code"] for f in out] == ["expiry_conflicts_perm"]

    def test_an_unexpired_exception_suppresses_the_breach_it_permits(self, tmp_path):
        """[expires:] is the one mechanism on this surface that works; a breach it
        explicitly permits must not be reported, or the detector punishes correct use."""
        base = _rules(tmp_path, "a",
                      "[perm] All cycling on weekdays only - no weekend rides.",
                      "[expires:2099-01-01] Saturday long rides permitted for the build.")
        out = rcf.check_athlete("a", base, events=[_ev(5, "Ride", "Long ride 5h")])
        assert [f["code"] for f in out] == ["expiry_conflicts_perm"]     # no day breach

    def test_an_expired_exception_no_longer_permits_it(self, tmp_path):
        base = _rules(tmp_path, "a",
                      "[perm] All cycling on weekdays only - no weekend rides.",
                      "[expires:2000-01-01] Saturday long rides permitted for the build.")
        out = rcf.check_athlete("a", base, events=[_ev(5, "Ride", "Long ride 5h")])
        assert "bike_prose_day_breach" in [f["code"] for f in out]


class TestReadOnly:
    def test_the_rule_file_is_never_modified(self, tmp_path):
        base = _rules(tmp_path, "a", LONG_RUN_WED, LONG_RIDE_FRI)
        f = base / "athletes" / "a" / "persistent-rules.md"
        before = f.read_bytes()
        rcf.check_athlete("a", base, cfg={"day_rules": {"run_days": ["Sat"]}},
                          events=[_ev(1, "Run", "Long run")])
        assert f.read_bytes() == before

    def test_findings_are_deduped(self, tmp_path):
        base = _rules(tmp_path, "a", LONG_RUN_WED, LONG_RUN_WED)
        out = rcf.check_athlete("a", base, events=[_ev(1, "Run", "Long run")])
        assert len(out) == 1
