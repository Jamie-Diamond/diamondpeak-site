"""A session name must not claim an intensity its steps never reach.

Every "defect" case here is a real session that reached Kathryn's live calendar on
4 Aug 2026. They were invisible until her ICU run threshold was set: before that,
each "% Pace" step resolved to nothing on the watch, so a "VO2" session with easy
steps simply gave her no targets. With a threshold set the same session prescribes
~7:00/km AS VO2, which is what made this worth a hard check.
"""
import unittest
from datetime import date

from primitives.planned_tss import (name_intensity_claim, name_intensity_mismatch,
                                    top_step_pct)
from primitives.validate_plan import validate_week


class ClaimDetection(unittest.TestCase):
    def test_most_demanding_claim_in_the_name_wins(self):
        # "VO2 + sweetspot" is judged on VO2, not on the easier half of the name.
        self.assertEqual(name_intensity_claim("Run", "VO2 Reps 6x3min + sweetspot")[0], "vo2")

    def test_claim_the_sport_has_no_band_for_is_not_asserted(self):
        self.assertIsNone(name_intensity_claim("Swim", "sweetspot 4x400"))

    def test_unclaimed_name_makes_no_demand(self):
        self.assertIsNone(name_intensity_claim("Run", "Long Run easy 80min"))

    def test_bar_is_the_band_lower_edge(self):
        # run threshold band is 95-101, so a threshold session must REACH 95.
        self.assertEqual(name_intensity_claim("Run", "Threshold 4x5min")[1], 95)


class BrickNaming(unittest.TestCase):
    """A brick lives on a Ride event but is named for both legs."""

    def test_ride_leg_is_not_judged_on_the_run_legs_claim(self):
        self.assertIsNone(name_intensity_mismatch(
            "Ride", "Brick: Z2 ride 70min + tempo run off the bike",
            "- 10m 55-65%\n- 50m 62-72%"))

    def test_run_leg_is_still_judged_on_its_own_claim(self):
        # "off the bike" is brick idiom, not another leg's claim: the tempo is this
        # run's, and 76-84% falls short of the run tempo band's 85% edge.
        mm = name_intensity_mismatch("Run", "Brick run: 15min tempo off the bike",
                                     "- 20m 76-84% Pace")
        self.assertEqual((mm["claim"], mm["required"]), ("tempo", 85))

    def test_run_leg_at_real_tempo_passes(self):
        self.assertIsNone(name_intensity_mismatch(
            "Run", "Brick run: 15min tempo off the bike", "- 15m 86-91% Pace"))

    def test_a_bike_claim_on_a_ride_still_counts(self):
        mm = name_intensity_mismatch("Ride", "Brick: Z2 ride + VO2 bike set + easy run",
                                     "- 54m 60-70%")
        self.assertEqual(mm["required"], 105)


class StepParsing(unittest.TestCase):
    def test_reads_top_of_each_range_and_separates_hr_from_pace(self):
        desc = "- 12m 82-88% Pace 84-89% LTHR\n- 5m 97-101% Pace 94-100% LTHR"
        self.assertEqual(top_step_pct(desc), (101, 100))

    def test_bare_percent_is_power_not_hr(self):
        self.assertEqual(top_step_pct("- 20m 95-102%"), (102, None))

    def test_sub_minute_steps_count(self):
        self.assertEqual(top_step_pct("- 30s 104-112% Pace"), (112, None))

    def test_prose_lines_are_not_steps(self):
        self.assertEqual(top_step_pct("Threshold reps - keep HR 180-190 bpm."), (None, None))


class RealDefects(unittest.TestCase):
    def test_vo2_run_with_no_hard_step(self):
        mm = name_intensity_mismatch(
            "Run", "VO2 Reps 6x3min + sweetspot",
            "- 2m 71-79% Pace\n- 8m 78-88% Pace\n- 18m 80-86% Pace\n- 15m 71-79% Pace")
        self.assertEqual((mm["claim"], mm["found"]), ("vo2", 88))

    def test_css_swim_that_never_reaches_css(self):
        mm = name_intensity_mismatch("Swim", "CSS 5x400",
                                     "- 15m 76-84% Pace\n- 32m 76-84% Pace")
        self.assertEqual(mm["claim"], "css")

    def test_brick_claiming_vo2_at_84_percent_ftp(self):
        mm = name_intensity_mismatch("Ride", "Brick: Z2 Ride + Tempo Run + VO2",
                                     "- 54m 60-70%\n- 15m 76-84%")
        self.assertEqual(mm["required"], 105)


class NoFalseAlarms(unittest.TestCase):
    def test_the_rebuilt_threshold_session_passes(self):
        self.assertIsNone(name_intensity_mismatch(
            "Run", "Run threshold 4x5min @ 180-190 bpm",
            "- 12m 82-88% Pace 84-89% LTHR\n- 5m 97-101% Pace 94-100% LTHR"))

    def test_an_hr_guardrail_cannot_excuse_an_easy_pace_target(self):
        # The one with teeth. Pace target + HR guardrail is now the house style, so if
        # HR could satisfy a claim outright this check would be off for every quality
        # session it exists to police.
        mm = name_intensity_mismatch("Run", "VO2 6x3min", "- 3m 80-86% Pace 94-100% LTHR")
        self.assertEqual((mm["claim"], mm["found"]), ("vo2", 86))

    def test_hr_targeted_quality_passes_without_a_pace_step(self):
        # %LTHR is not comparable with %pace: VO2 work barely exceeds threshold HR,
        # so a step at threshold HR satisfies any claim.
        self.assertIsNone(name_intensity_mismatch("Run", "VO2 6x3min", "- 3m 100-102% LTHR"))

    def test_genuine_bike_threshold_passes(self):
        self.assertIsNone(name_intensity_mismatch("Ride", "Threshold 2x20",
                                                  "- 10m 60-70%\n- 20m 95-102%"))

    def test_unstructured_session_is_left_to_the_no_steps_check(self):
        self.assertIsNone(name_intensity_mismatch("Run", "VO2 6x3min", ""))

    def test_easy_session_never_flags(self):
        self.assertIsNone(name_intensity_mismatch("Run", "Easy Run (HR-capped)",
                                                  "- 70m 78-88% Pace"))


class ValidatorWiring(unittest.TestCase):
    """The check has to be HARD in validate_week, else Stage 1 keeps the attempt."""

    def _event(self, name, desc):
        return {"start_date_local": "2026-08-05T00:00:00", "type": "Run",
                "category": "WORKOUT", "load_target": 50, "moving_time": 2880,
                "name": name, "description": desc}

    def test_mismatch_is_a_hard_violation(self):
        rep = validate_week([self._event("VO2 Reps 6x3min", "- 18m 80-86% Pace")],
                            date(2026, 8, 3))
        codes = [v.code for v in rep.violations if v.severity == "hard"]
        self.assertIn("name_intensity_mismatch", codes)

    def test_coherent_session_raises_nothing(self):
        rep = validate_week([self._event("Run threshold 4x5min", "- 5m 97-101% Pace")],
                            date(2026, 8, 3))
        self.assertNotIn("name_intensity_mismatch", [v.code for v in rep.violations])

    def test_events_without_rendered_steps_do_not_trip_it(self):
        # A caller that passes only coaching prose must not get a phantom failure.
        e = self._event("VO2 Reps 6x3min", "")
        e["description_raw"] = "VO2 session, 6x3min hard off the bike."
        rep = validate_week([e], date(2026, 8, 3))
        self.assertNotIn("name_intensity_mismatch", [v.code for v in rep.violations])


if __name__ == "__main__":
    unittest.main()
