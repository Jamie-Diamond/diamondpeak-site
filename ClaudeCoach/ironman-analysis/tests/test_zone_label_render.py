"""Canonical TID band labels (Z1-Z5c) must render as the intensity they name.

Every case here is from Sunday 9 Aug 2026, when the weekly generator produced NO plan
for ANY of the three athletes: each attempt hard-blocked on name_intensity_mismatch. The
planning brief hands Stage 1 the library's zone LABEL for each quality type (bike
threshold = "Z4", swim css = "Z4", bike vo2 = "Z5"), so proposals came back labelled Z4 /
Z5 - a vocabulary _ZONE_BAND did not carry. They fell through to _ZONE_DEFAULT_IF, so the
steps rendered at the sport's EASY default and the found values landed on the two
give-away numbers: 72% FTP (bike 0.68 default) and 84% (swim 0.80 default).

The check was right and the render was wrong, so these tests pin the RENDER. The last
class is the one with teeth: a name that genuinely over-claims must still block, or this
fix would have switched the check off rather than fixed it.
"""
import unittest
from datetime import date

from primitives.planned_tss import (_TID_ALIAS, name_intensity_mismatch, render_workout,
                                    segment_if, top_step_pct)
from primitives.validate_plan import validate_week


def _hardest(sport, name, segments):
    """(hardest %-target, mismatch) for a session rendered the way plan_builder does."""
    r = render_workout(sport, segments, name)
    return top_step_pct(r["description"])[0], name_intensity_mismatch(sport, name, r["description"])


class LabelIFsAgreeWithStage1(unittest.TestCase):
    """The literals from scripts/stage1-plan.py:_ZLABEL_IF, which scores the same labels
    for the intensity distribution. Written out rather than imported: two tables that must
    agree are only pinned by asserting the numbers."""

    def test_quality_labels(self):
        for sport, label, want in (("Ride", "Z4", 0.95), ("Ride", "Z5", 1.05),
                                   ("Run", "Z4", 0.97), ("Swim", "Z4", 1.00)):
            self.assertAlmostEqual(segment_if(sport, label), want, places=2,
                                   msg=f"{sport} {label}")

    def test_easy_labels(self):
        for sport, label, want in (("Ride", "Z2", 0.65), ("Run", "Z2", 0.83),
                                   ("Swim", "Z2", 0.72), ("Swim", "Z1", 0.60)):
            self.assertAlmostEqual(segment_if(sport, label), want, places=2,
                                   msg=f"{sport} {label}")

    def test_no_canonical_label_falls_back_to_the_sport_default(self):
        # The fallback IS the bug: bike 0.68 -> 64-72%, swim 0.80 -> 76-84%.
        for sport, sp, dflt, labels in (
                ("Ride", "bike", 0.68, ("Z1", "Z2", "Z3", "Z4", "Z5")),
                ("Run", "run", 0.75, ("Z1", "Z2", "Z3", "Z4", "Z5a", "Z5b", "Z5c")),
                ("Swim", "swim", 0.80, ("Z1", "Z2", "Z3", "Z4", "Z5a", "Z5b", "Z5c"))):
            for label in labels:
                got = round(segment_if(sport, label), 2)
                self.assertNotEqual(got, dflt,
                                    msg=f"{sport} {label} -> {got} is the {sp} sport default")
                self.assertTrue(label.lower() in _TID_ALIAS[sp] or label.lower() in ("z1", "z2", "z3"),
                                msg=f"{sport} {label} resolves only by luck")


class TheSevenBlockedSessions(unittest.TestCase):
    """One per distinct claim word that blocked: css, threshold, sweetspot, vo2."""

    def test_css_swim_reaches_css(self):
        # jamie 'CSS set - 4x300' (Swim): library swim css is Z4; found 84%, needed 97%.
        found, mm = _hardest("Swim", "CSS set - 4x300",
                             [{"minutes": 10, "zone": "Z1"},
                              {"repeat": 4, "steps": [{"minutes": 5, "zone": "Z4"},
                                                      {"minutes": 1, "zone": "Z1"}]}])
        self.assertIsNone(mm)
        self.assertEqual(found, 103)

    def test_threshold_ride_reaches_threshold(self):
        # kathryn 'Threshold 3x8' / calum 'Threshold 4x10' (Bike): Z4; found 72%, needed 95%.
        found, mm = _hardest("Ride", "Threshold 3x8",
                             [{"minutes": 15, "zone": "Z2"},
                              {"repeat": 3, "steps": [{"minutes": 8, "zone": "Z4"},
                                                      {"minutes": 4, "zone": "Z2"}]}])
        self.assertIsNone(mm)
        self.assertEqual(found, 102)

    def test_sweetspot_ride_reaches_sweetspot(self):
        # jamie 'Sweetspot 2x20' (Bike): the library gives tempo AND sweetspot the label Z3,
        # so the label alone cannot decide - the NAME narrows it to 88-94% FTP.
        found, mm = _hardest("Ride", "Sweetspot 2x20",
                             [{"minutes": 15, "zone": "Z2"},
                              {"repeat": 2, "steps": [{"minutes": 20, "zone": "Z3"},
                                                      {"minutes": 6, "zone": "Z2"}]}])
        self.assertIsNone(mm)
        self.assertEqual(found, 94)

    def test_a_z3_ride_NOT_named_sweetspot_stays_tempo(self):
        # The narrowing is driven by the name and nothing else: no sweetspot claim, no 88-94%.
        r = render_workout("Ride", [{"minutes": 20, "zone": "Z3"}], "Tempo 1x20")
        self.assertEqual(top_step_pct(r["description"])[0], 84)

    def test_vo2_touch_reaches_vo2(self):
        # jamie 'Endurance + VO2 touch' / '+ VO2 short' (Bike): Z5; found 72%, needed 105%.
        found, mm = _hardest("Ride", "Endurance + VO2 touch",
                             [{"minutes": 60, "zone": "Z2"},
                              {"repeat": 3, "steps": [{"minutes": 3, "zone": "Z5"},
                                                      {"minutes": 3, "zone": "Z2"}]}])
        self.assertIsNone(mm)
        self.assertEqual(found, 118)

    def test_whole_week_validates(self):
        """All seven, as one week through validate_week - the gate that produced no plan."""
        sessions = [
            ("2026-08-10", "Swim", "CSS set - 4x300", [{"minutes": 10, "zone": "Z1"},
                                                       {"minutes": 20, "zone": "Z4"}]),
            ("2026-08-11", "Swim", "CSS set - 6x300", [{"minutes": 10, "zone": "Z1"},
                                                       {"minutes": 30, "zone": "Z4"}]),
            ("2026-08-11", "Ride", "Threshold 3x8", [{"minutes": 15, "zone": "Z2"},
                                                     {"minutes": 24, "zone": "Z4"}]),
            ("2026-08-12", "Ride", "Threshold 4x10", [{"minutes": 15, "zone": "Z2"},
                                                      {"minutes": 40, "zone": "Z4"}]),
            ("2026-08-13", "Ride", "Sweetspot 2x20", [{"minutes": 15, "zone": "Z2"},
                                                      {"minutes": 40, "zone": "Z3"}]),
            ("2026-08-14", "Ride", "Endurance + VO2 touch", [{"minutes": 60, "zone": "Z2"},
                                                             {"minutes": 9, "zone": "Z5"}]),
            ("2026-08-15", "Ride", "Endurance + VO2 short", [{"minutes": 75, "zone": "Z2"},
                                                             {"minutes": 8, "zone": "Z5"}]),
        ]
        events = []
        for d, sport, name, segs in sessions:
            r = render_workout(sport, segs, name)
            events.append({"start_date_local": f"{d}T00:00:00", "type": sport,
                           "category": "WORKOUT", "load_target": r["tss"],
                           "moving_time": r["duration_min"] * 60,
                           "name": name, "description": r["description"]})
        rep = validate_week(events, date(2026, 8, 10))
        # NOT just the one code: the Sunday gate is stage1-plan's `n_blocking == 0`, which
        # takes EVERY hard violation, so a week that only clears this check still pushes
        # nothing.
        self.assertEqual([(v.code, v.detail) for v in rep.violations
                          if v.severity == "hard"], [])


class InjectedDoses(unittest.TestCase):
    """quality_inject writes the label and the name itself, so its output is a second
    producer of steps and blocked the week the same way."""

    def test_vo2_dose_label_matches_its_own_name(self):
        import sys, pathlib
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "lib"))
        import quality_inject as qi
        found, mm = _hardest("Bike", f"Endurance ride + {qi._DOSE_NAME['high']}",
                             [{"minutes": 90, "zone": "z2"},
                              {"minutes": 12, "zone": qi._SEG_ZONE["high"]}])
        self.assertIsNone(mm)
        self.assertEqual(found, 118)

    def test_sweetspot_dose_label_matches_its_own_name(self):
        import sys, pathlib
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "lib"))
        import quality_inject as qi
        found, mm = _hardest("Bike", f"Endurance ride + {qi._DOSE_NAME['z3']}",
                             [{"minutes": 90, "zone": "z2"},
                              {"minutes": 25, "zone": qi._SEG_ZONE["z3"]}])
        self.assertIsNone(mm)
        self.assertEqual(found, 94)


class StillHasTeeth(unittest.TestCase):
    """The narrowing is bounded to the label's OWN canonical band. A name claiming an
    intensity the segments are nowhere near is an over-claim, and must still block -
    otherwise this fix would have disabled the check instead of fixing the render."""

    def test_vo2_over_easy_segments_still_blocks(self):
        found, mm = _hardest("Ride", "VO2 6x3min", [{"minutes": 20, "zone": "Z2"},
                                                    {"minutes": 40, "zone": "Z2"}])
        self.assertEqual((mm["claim"], mm["required"], mm["found"]), ("vo2", 105, 70))

    def test_threshold_over_a_z3_segment_still_blocks(self):
        # Z3 tops out at 90% FTP canonically; threshold needs 95%. Different family.
        _, mm = _hardest("Ride", "Threshold 3x10", [{"minutes": 30, "zone": "Z3"}])
        self.assertEqual((mm["claim"], mm["found"]), ("threshold", 84))

    def test_refinement_never_softens_a_harder_label(self):
        # Z5c is the library's run `reps` (112-120% pace) and already clears a VO2 claim.
        # Narrowing must not pull it DOWN to the vo2 band's 103-110%.
        found, mm = _hardest("Run", "VO2 8x400", [{"minutes": 15, "zone": "Z2"},
                                                  {"minutes": 12, "zone": "Z5c"}])
        self.assertIsNone(mm)
        self.assertEqual(found, 120)

    def test_css_swim_over_easy_labels_still_blocks(self):
        _, mm = _hardest("Swim", "CSS 5x400", [{"minutes": 15, "zone": "Z1"},
                                               {"minutes": 32, "zone": "Z2"}])
        self.assertEqual((mm["claim"], mm["found"]), ("css", 80))


class ExplicitIntensityWins(unittest.TestCase):
    def test_an_if_beats_a_coarse_label(self):
        # tss_from_segments has always let an explicit `if` win; the band table did not,
        # so {"zone":"Z3","if":0.9} was scored at 0.90 and pushed at 76-84%.
        r = render_workout("Ride", [{"minutes": 20, "zone": "Z3", "if": 0.9}], "Sweetspot 1x20")
        self.assertEqual(top_step_pct(r["description"])[0], 94)

    def test_a_named_system_zone_keeps_its_band(self):
        # NOT the same rule: an `if` that undershoots a named zone must not quietly
        # under-prescribe it (and hard-block the week on the name's claim).
        r = render_workout("Ride", [{"minutes": 20, "zone": "threshold", "if": 0.9}],
                           "Threshold 1x20")
        self.assertEqual(top_step_pct(r["description"])[0], 102)


class WithoutAName(unittest.TestCase):
    """plan_tools' manual render-workout CLI passes no name; the alias alone must still
    render a Z4/Z5 label as quality."""

    def test_labels_resolve_with_no_name(self):
        r = render_workout("Ride", [{"minutes": 10, "zone": "Z4"}, {"minutes": 3, "zone": "Z5"}])
        self.assertEqual(top_step_pct(r["description"])[0], 118)

    def test_swim_warmup_label_is_not_near_css(self):
        # The quieter half of the same fallback: every TID-labelled swim warm-up was
        # prescribed at 76-84% of CSS.
        r = render_workout("Swim", [{"minutes": 10, "zone": "Z1"}])
        self.assertEqual(top_step_pct(r["description"])[0], 66)


if __name__ == "__main__":
    unittest.main()
