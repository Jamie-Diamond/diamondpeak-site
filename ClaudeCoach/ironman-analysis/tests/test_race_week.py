"""Race week — the week that contains the race.

Nothing anywhere costed the race. `validate_week` is only ever passed the BUILT
proposal, so the race event sat outside the week total, outside the load cap and
outside the CTL-ramp projection; and the taper ladder's final step (40% of
maintenance) — a WHOLE-WEEK volume target — was handed to the planner as a
TRAINING budget. For a CTL-110 Ironman athlete that is ~300 TSS of training in
the seven days around a ~540 TSS race, and nothing stopped a session landing on
race day itself.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import plan_tools as pt  # noqa: E402
from primitives.planned_tss import race_tss, _RACE_PROFILE  # noqa: E402
from primitives.validate_plan import validate_week  # noqa: E402

RACE = date(2026, 9, 19)          # a Saturday
MONDAY = date(2026, 9, 14)        # Monday of race week

CFG = {
    "plan_start": "2026-05-04",
    "race_date": RACE.isoformat(),
    "race_name": "IM Italy",
    "race_distance": "Full Ironman",
    "phase_tss": {"base_end_week": 8, "build_end_week": 14, "peak_end_week": 18},
    "ctl_targets": {"phase_ctl": {"base": 85, "build": 100, "peak": 115}},
    "max_ctl_ramp_per_week": 5.0,
}
CTL = 108.0
MAINT = 7 * CTL


def _req(day, cfg=None, ctl=CTL):
    return pt.required_tss(cfg or CFG, ctl, today=day)


class TestRaceLoadEstimate:
    def test_event_defaults_land_where_the_distances_actually_cost(self):
        assert race_tss("Full Ironman")[0] == 539       # ~11 h at IF 0.70
        assert race_tss("70.3")[0] == 320               # ~5 h at IF 0.80
        assert race_tss("Olympic")[0] == 178

    def test_they_are_ordered_by_distance(self):
        got = [race_tss(e)[0] for e in ("Sprint", "Olympic", "70.3", "Full Ironman")]
        assert got == sorted(got)

    def test_cycling_events_share_the_sportive_profile(self):
        assert race_tss("Gran Fondo") == race_tss("Sportive") == race_tss("Gravel")

    def test_an_explicit_figure_beats_the_estimate(self):
        assert race_tss("Full Ironman", expected_tss=430) == (430, "explicit")

    def test_a_known_finish_time_beats_the_event_default(self):
        tss, src = race_tss("Full Ironman", expected_hours=10.5)
        assert src == "duration"
        assert tss == round(10.5 * 100 * 0.70 ** 2)
        assert tss < race_tss("Full Ironman")[0]        # faster race, less load

    def test_an_unknown_event_still_returns_something_usable(self):
        tss, src = race_tss("Backyard Ultra Thing")
        assert tss > 0 and src == "event_default"

    def test_config_overrides_route_through(self):
        assert pt.race_load(dict(CFG, race_tss=430)) == (430, "explicit")
        assert pt.race_load(dict(CFG, race_expected_hours=10.5))[1] == "duration"
        hours, IF = _RACE_PROFILE["Full Ironman"]
        assert pt.race_load(CFG) == (round(hours * 100 * IF ** 2), "event_default")


class TestRaceWeekBudget:
    def test_the_week_is_typed_as_a_race_week(self):
        r = _req(MONDAY)
        assert r["week_type"] == "race"
        assert r["race_in_week"] is True
        assert r["phase"] == "taper"          # still inside the taper phase

    def test_the_race_is_costed_and_deducted(self):
        r = _req(MONDAY)
        assert r["race_tss_estimate"] == 539
        assert r["whole_week_tss_incl_race"] == round(MAINT * pt._TAPER_FACTORS[1])
        assert r["recommended_weekly_tss"] < r["whole_week_tss_incl_race"]

    def test_training_falls_to_the_openers_floor_for_a_long_race(self):
        """The race alone exceeds the whole-week figure — openers is the right answer."""
        r = _req(MONDAY)
        assert r["training_at_floor"] is True
        assert r["recommended_weekly_tss"] == round(MAINT * pt._RACE_WEEK_MIN)

    def test_a_short_race_leaves_room_for_more_than_the_floor(self):
        cfg = dict(CFG, race_distance="Olympic")
        r = _req(MONDAY, cfg)
        assert r["race_tss_estimate"] == 178
        assert r["training_at_floor"] is False
        assert r["recommended_weekly_tss"] == round(MAINT * pt._TAPER_FACTORS[1]) - 178

    def test_the_old_behaviour_is_gone(self):
        """It used to prescribe the whole-week figure as TRAINING, on top of the race."""
        r = _req(MONDAY)
        assert r["recommended_weekly_tss"] != round(MAINT * pt._TAPER_FACTORS[1])
        assert r["recommended_weekly_tss"] < r["race_tss_estimate"]

    def test_the_note_forbids_the_things_that_ruin_a_race(self):
        note = _req(MONDAY)["note"]
        assert "RACE WEEK" in note
        assert "Nothing on race day" in note
        for banned in ("long ride", "long run", "quality"):
            assert banned in note

    def test_it_fires_from_any_day_inside_race_week(self):
        for offset in range(0, 6):            # Mon..Sat, race on the Sat
            assert _req(MONDAY + timedelta(days=offset))["week_type"] == "race"

    def test_it_does_not_fire_the_week_before(self):
        r = _req(MONDAY - timedelta(days=7))
        assert r["week_type"] == "taper"
        assert "race_tss_estimate" not in r

    def test_no_floor_is_imposed_on_a_race_week(self):
        assert _req(MONDAY)["weekly_tss_floor"] == 0

    def test_a_race_week_does_not_read_as_a_missed_week_afterwards(self):
        """week_type 'race' was already in plan_tools' intrinsic-down-week list; before
        this nothing ever set it, so the guard could not fire."""
        assert "race" in ("deload", "taper", "race", "post_race")
        light = 0.2 * MAINT
        cfg = dict(CFG, race_date=(MONDAY + timedelta(days=5)).isoformat())
        r = pt.required_tss(cfg, CTL, today=MONDAY + timedelta(days=7),
                            last_week_tss=light)
        assert "deload_reason" not in r


class TestNothingOnRaceDay:
    def _ev(self, d, name="Session", load=40):
        return {"category": "WORKOUT", "type": "Ride", "name": name,
                "start_date_local": f"{d}T07:00", "load_target": load}

    def test_a_session_on_race_day_is_a_hard_violation(self):
        rep = validate_week([self._ev(RACE, "Openers")], MONDAY, race_date=RACE)
        v = [x for x in rep.violations if x.code == "session_on_race_day"]
        assert v and v[0].severity == "hard"

    def test_the_day_before_is_fine(self):
        rep = validate_week([self._ev(RACE - timedelta(days=1))], MONDAY, race_date=RACE)
        assert not [x for x in rep.violations if x.code == "session_on_race_day"]

    def test_the_check_is_opt_in_like_every_other(self):
        rep = validate_week([self._ev(RACE)], MONDAY)          # no race_date supplied
        assert not [x for x in rep.violations if x.code == "session_on_race_day"]

    def test_a_race_outside_the_week_is_not_asserted_on(self):
        rep = validate_week([self._ev(MONDAY)], MONDAY, race_date=RACE + timedelta(days=30))
        assert not [x for x in rep.violations if x.code == "session_on_race_day"]

    def test_the_code_is_classified_as_a_safety_blocker(self):
        """Otherwise the empty-week fallback would push the week anyway."""
        src = (REPO / "scripts" / "stage1-plan.py").read_text()
        block = src.split("_SAFETY_BLOCKER_CODES = frozenset({")[1].split("})")[0]
        assert "session_on_race_day" in block


class TestDownWeekTypesAreOneDefinition:
    """Race and post-race weeks are light BY DESIGN and must be treated as down-weeks
    everywhere, not just where the tuple happened to be written out.

    stage1-plan asks "is this a down-week?" in three places to decide whether to force
    a minimum quality dose, run the intensity-budget check and relax the zone-deviation
    floors. All three said `("deload", "taper")`, so a race week — and the week after an
    Ironman — would have been handed a normal week's quality requirement.
    """

    def test_the_list_covers_every_light_by_design_week(self):
        assert set(pt.DOWN_WEEK_TYPES) == {"deload", "taper", "race", "post_race"}

    def test_stage1_uses_the_shared_list_everywhere(self):
        src = (REPO / "scripts" / "stage1-plan.py").read_text()
        assert src.count("pt.DOWN_WEEK_TYPES") == 3
        assert '("deload", "taper")' not in src

    def test_every_week_type_the_engine_emits_is_classified(self):
        """A new week_type that nobody adds to DOWN_WEEK_TYPES should at least be a
        deliberate omission, so pin the full set the engine can produce."""
        emitted = set()
        cfg = dict(CFG, deload_every_n_weeks=4)
        d = date(2026, 5, 4)
        while d < RACE + timedelta(days=60):
            r = pt.required_tss(cfg, CTL, today=d)
            if r.get("week_type"):
                emitted.add(r["week_type"])
            d += timedelta(days=7)
        assert emitted <= {"base", "build", "specific", "peak", "deload",
                           "taper", "race", "post_race"}
        assert {"taper", "race", "post_race"} <= emitted


class TestRaceLoadFromTheAthletesOwnRace:
    """A real race beats a table. Jamie's IM Italy comes to 556 TSS summed per leg,
    against a 539 event default that was never his."""

    PROFILE = {
        "swim_css_per_100m": "1:39",
        "run_threshold_pace_per_km": "4:02",
        "prev_race": {"name": "IM Italy Emilia-Romagna", "date": "2025-09-20",
                      "swim_time": "1:09", "bike_time": "4:55", "bike_np_watts": 201,
                      "bike_if": 0.73, "run_time": "3:52", "run_pace": "5:37/km"},
    }

    def test_prev_race_beats_the_event_default(self):
        tss, src = pt.race_load(CFG, self.PROFILE)
        assert src == "prev_race"
        assert tss == 556
        assert tss != race_tss("Full Ironman")[0]

    def test_it_sums_per_leg_not_one_blended_if(self):
        from primitives.planned_tss import race_tss_from_prev_race
        tss, _ = race_tss_from_prev_race(
            self.PROFILE["prev_race"], self.PROFILE["swim_css_per_100m"],
            self.PROFILE["run_threshold_pace_per_km"])
        bike_only = 295 / 60 * 0.73 ** 2 * 100
        assert tss > bike_only * 2          # swim and run are both really in there

    def test_the_recorded_bike_if_is_used_not_one_recomputed_today(self):
        """201 W against today's FTP reads 0.65, not the 0.73 it was — repricing last
        year's race at this year's fitness loses ~70 TSS."""
        from primitives.planned_tss import race_tss_from_prev_race
        as_raced, _ = race_tss_from_prev_race(self.PROFILE["prev_race"])
        stale = dict(self.PROFILE["prev_race"], bike_if=201 / 307)
        repriced, _ = race_tss_from_prev_race(stale)
        assert as_raced - repriced > 50

    def test_an_explicit_figure_still_wins(self):
        assert pt.race_load(dict(CFG, race_tss=500), self.PROFILE) == (500, "explicit")

    def test_a_different_distance_is_not_borrowed(self):
        cfg = dict(CFG, race_distance="70.3", race_name="70.3 Somewhere")
        assert pt.race_load(cfg, self.PROFILE)[1] == "event_default"

    def test_no_profile_falls_back_cleanly(self):
        assert pt.race_load(CFG)[1] == "event_default"

    def test_a_malformed_prev_race_does_not_raise(self):
        for bad in ({"bike_time": "nonsense", "bike_if": 0.73}, {"bike_if": 0.73},
                    {"bike_time": "4:55"}, {}):
            assert pt.race_load(CFG, {"prev_race": bad})[1] == "event_default"


class TestOpenersAreAboveRaceIntensity:
    """Long-course race intensity sits at or below the top of Z2 — Jamie's IM bike was
    230 W against an FTP of 307, exactly 75% of FTP. "A few minutes at race effort" in
    race-week openers therefore prescribes something easier than his normal easy riding.
    """

    LONG = {"prev_race": {"bike_time": "4:55", "bike_if": 0.636}}     # IM, sub-Z2-top
    SHORT = {"prev_race": {"bike_time": "1:00", "bike_if": 0.92}}     # sprint/olympic

    def test_long_course_openers_are_explicitly_above_race_effort(self):
        note = pt.required_tss(CFG, CTL, today=MONDAY, profile=self.LONG)["note"]
        assert "ABOVE race intensity" in note
        assert "threshold/VO2" in note
        assert "is NOT a stimulus" in note

    def test_short_course_openers_use_race_pace(self):
        cfg = dict(CFG, race_distance="Olympic")
        note = pt.required_tss(cfg, CTL, today=MONDAY, profile=self.SHORT)["note"]
        assert "race pace IS the sharpening intensity" in note

    def test_the_taper_note_no_longer_calls_race_pace_intensity(self):
        cfg = dict(CFG, phase_tss=dict(CFG["phase_tss"], peak_end_week=14))
        note = pt.required_tss(cfg, CTL, today=MONDAY - timedelta(days=7),
                               profile=self.LONG)["note"]
        assert "THRESHOLD and above" in note
        assert "not intensity" in note

    def test_it_falls_back_to_the_event_when_there_is_no_prev_race(self):
        assert pt.race_intensity(CFG) <= pt._LONG_COURSE_IF          # Full Ironman
        assert pt.race_intensity({"race_distance": "Sprint"}) > pt._LONG_COURSE_IF
