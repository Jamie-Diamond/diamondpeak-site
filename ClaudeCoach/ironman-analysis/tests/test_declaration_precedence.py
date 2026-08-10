"""Declaration vs standing day_rules — the precedence, and the week it applies to.

THE TRANSCRIPT THESE TESTS COME FROM (Jamie, 10 Aug 2026). He had declared his week on
1 Aug. The coach acknowledged it, then an hour later deleted it as junk because it clashed
with his standing `day_rules`, rebuilt the week off the day rules, and told him a session
was his own declaration when nothing of his said so. Three times in one conversation:
"Are you stupid... I already told you what my availability was for this week. Find it and
sort your shit out."

The mechanism was NOT a parser that misread him - `parse_day_shape_message` reads his
sentence correctly, and the first test here pins that. It was:

  1. SCOPING. He restated the week on a MONDAY. `target_week` falls an unframed message
     through to the NEXT Monday, so the declaration was stored against w/c 17 Aug,
     `day_shape()` for the week in progress returned None, and the generator saw no
     declaration at all - which is where the standing Wednesday run and Friday ride came
     back from.
  2. PER-SPORT rather than PER-DAY precedence. The old merge replaced `swim_days` and left
     `run_days` alone, so "Wednesday swim" kept the standing Wednesday run, and
     `swim_focus` kept a CSS swim on the Thursday he had given to a long ride.

Every assertion below is against his real declaration text and his real config day_rules.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))
sys.path.insert(0, str(REPO / "ironman-analysis"))

import weekly_availability as wa                               # noqa: E402
import session_library as sl                                   # noqa: E402
from primitives.validate_plan import validate_week             # noqa: E402

# Jamie, 1 Aug 2026 — the only declaration he had given, verbatim.
DECL = ("Monday rest, Tuesday swim morning long run evening, Wednesday swim (will be "
        "tired after run). Thursday long ride. Friday/Saturday run. Sunday rest "
        "(travelling).")

# Jamie's real config/athletes.json day_rules.
DAY_RULES = {"swim_days": ["Tue", "Thu"],
             "bike_days": ["Fri", "Sat", "Sun"],
             "run_days": ["Tue", "Wed", "Sat", "Sun"],
             "strength_max": 2,
             "swim_focus": {"Tue": ["technique", "speed"], "Thu": ["css"]}}

MON = date(2026, 8, 10)          # the Monday of the argument
NEXT_MON = date(2026, 8, 17)     # the week his declaration was wrongly recorded against


def _parsed():
    return wa.parse_day_shape_message(DECL)


def _shape():
    p = _parsed()
    return {k: p[k] for k in ("swim_days", "bike_days", "run_days", "unavailable_days")} | \
           {"declared_days": p["named_days"]}


class TestTheParserDidNotInventASport:
    """(a) "Wednesday swim" is a swim and nothing else. This ALREADY held; it is pinned
    here because the coach quoted an invented "swim + bike Wednesday" back at him as his
    own words, and a parser regression would make that true rather than merely claimed."""

    def test_wednesday_is_swim_only(self):
        p = _parsed()
        assert "Wed" in p["swim_days"]
        assert "Wed" not in p["bike_days"]
        assert "Wed" not in p["run_days"]

    def test_every_named_day_is_reported(self):
        # All seven, so the merge knows day_rules have nothing left to decide.
        assert _parsed()["named_days"] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    def test_the_parenthesised_aside_is_not_a_run(self):
        # "(will be tired after run)" sits inside Wednesday's segment.
        assert _parsed()["run_days"] == ["Tue", "Fri", "Sat"]


class TestDeclarationOutranksDayRules:
    """(b) Every named day comes from the declaration."""

    def test_the_whole_week_is_his(self):
        dr, _ = wa.merge_day_rules(DAY_RULES, _shape())
        assert dr["swim_days"] == ["Tue", "Wed"]
        assert dr["bike_days"] == ["Thu"]
        assert dr["run_days"] == ["Tue", "Fri", "Sat"]

    def test_the_standing_wednesday_run_is_gone(self):
        # day_rules.run_days has Wed. He gave Wednesday to a swim.
        dr, _ = wa.merge_day_rules(DAY_RULES, _shape())
        assert "Wed" not in dr["run_days"]

    def test_the_standing_friday_ride_is_gone(self):
        # "I said Friday Saturday run not ride" (1 Aug 2026).
        dr, _ = wa.merge_day_rules(DAY_RULES, _shape())
        assert "Fri" not in dr["bike_days"]
        assert "Sat" not in dr["bike_days"]

    def test_swim_focus_does_not_re_add_a_swim_to_his_ride_day(self):
        # The second place a sport can land on a day: {"Thu": ["css"]} against a declared
        # long ride. Pruning it is what stops a swim appearing on Thursday.
        dr, _ = wa.merge_day_rules(DAY_RULES, _shape())
        assert "Thu" not in dr["swim_focus"]
        assert dr["swim_focus"] == {"Tue": ["technique", "speed"]}

    def test_rest_days_carry_nothing(self):
        dr, _ = wa.merge_day_rules(DAY_RULES, _shape())
        for key in ("swim_days", "bike_days", "run_days"):
            assert "Mon" not in dr[key]
            assert "Sun" not in dr[key]

    def test_unnamed_days_still_come_from_day_rules(self):
        # A partial declaration: three days named, four untouched. day_rules fill the rest,
        # and nothing infers that the days he did not mention are unavailable - the 3 Aug
        # failure in the opposite direction ("Thursday is the only bike day", which he
        # never said).
        p = wa.parse_day_shape_message("Monday rest, Tuesday swim, Wednesday swim.")
        shape = {k: p[k] for k in ("swim_days", "bike_days", "run_days", "unavailable_days")}
        shape["declared_days"] = p["named_days"]
        dr, _ = wa.merge_day_rules(DAY_RULES, shape)
        assert shape["declared_days"] == ["Mon", "Tue", "Wed"]
        assert dr["bike_days"] == ["Fri", "Sat", "Sun"]        # untouched, none named
        assert dr["run_days"] == ["Sat", "Sun"]                # Tue/Wed his, Sat/Sun standing
        assert dr["swim_days"] == ["Tue", "Wed", "Thu"]        # Thu never mentioned

    def test_no_declaration_changes_nothing(self):
        dr, conflicts = wa.merge_day_rules(DAY_RULES, None)
        assert dr == DAY_RULES and conflicts == []

    def test_reconcile_day_rules_is_the_same_answer(self):
        # session_library delegates rather than carrying a second copy of the precedence.
        assert sl.reconcile_day_rules(DAY_RULES, _shape()) == \
            wa.merge_day_rules(DAY_RULES, _shape())[0]


class TestScopedToOneWeek:
    """(c) A declaration applies to the week it names and no other."""

    def test_it_does_not_leak_into_the_following_week(self, tmp_path):
        wa.record("jamie", MON, base=tmp_path, source="test", **_shape())
        assert wa.day_shape("jamie", MON, base=tmp_path) is not None
        assert wa.for_week("jamie", NEXT_MON, base=tmp_path) is None
        assert wa.day_shape("jamie", NEXT_MON, base=tmp_path) is None

    def test_next_week_is_planned_off_day_rules_again(self, tmp_path):
        # The travel week must not become every week: a one-off leaking forward is the
        # equal and opposite failure.
        wa.record("jamie", MON, base=tmp_path, source="test", **_shape())
        dr, _ = wa.effective_day_rules("jamie", NEXT_MON, DAY_RULES, base=tmp_path)
        assert dr == DAY_RULES

    def test_declared_days_survive_the_round_trip(self, tmp_path):
        wa.record("jamie", MON, base=tmp_path, source="test", **_shape())
        assert wa.day_shape("jamie", MON, base=tmp_path)["declared_days"] == \
            ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class TestWhichWeekADayShapeIsAbout:
    """The scoping bug itself: an unframed day shape typed mid-week is about the week in
    progress, not the next one."""

    def test_monday_restatement_lands_on_the_current_week(self, tmp_path):
        got = wa.day_shape_target_week("jamie", DECL, named_days=_parsed()["named_days"],
                                       today=MON, base=tmp_path)
        assert got == MON, "the 10 Aug 2026 bug: recorded against w/c 17 Aug"

    def test_wednesday_restatement_lands_on_the_current_week(self, tmp_path):
        assert wa.day_shape_target_week("jamie", DECL, named_days=["Thu", "Fri", "Sat"],
                                        today=date(2026, 8, 12),
                                        base=tmp_path) == MON

    def test_next_week_wording_wins(self, tmp_path):
        assert wa.day_shape_target_week(
            "jamie", "Next week: Monday rest, Tuesday swim, Wednesday long run.",
            named_days=["Mon", "Tue", "Wed"], today=MON, base=tmp_path) == NEXT_MON

    def test_friday_falls_through_to_next_week(self, tmp_path):
        # From Friday there is not enough of the week left for a whole-week shape to be
        # about it, so the Sunday reading (next Monday) is the right default.
        assert wa.day_shape_target_week("jamie", DECL, named_days=_parsed()["named_days"],
                                        today=date(2026, 8, 14),
                                        base=tmp_path) == NEXT_MON

    def test_a_shape_naming_only_past_days_is_next_week(self, tmp_path):
        # Thursday, naming Mon-Wed: he cannot be declaring days that have gone.
        assert wa.day_shape_target_week("jamie", "Monday rest, Tuesday swim, Wednesday run.",
                                        named_days=["Mon", "Tue", "Wed"],
                                        today=date(2026, 8, 13), base=tmp_path) == NEXT_MON

    def test_an_outstanding_ask_still_names_the_week(self, tmp_path):
        # A recorded fact about what was actually asked beats any calendar inference.
        wa.note_ask_sent("jamie", NEXT_MON, base=tmp_path)
        assert wa.day_shape_target_week("jamie", DECL, named_days=_parsed()["named_days"],
                                        today=MON, base=tmp_path) == NEXT_MON

    def test_the_hours_resolver_is_untouched(self, tmp_path):
        # target_week answers a different question (a reply to the Sunday hours ask, whose
        # subject IS next week) and must keep falling through to the next Monday.
        assert wa.target_week("jamie", "14", today=MON, base=tmp_path) == NEXT_MON


class TestConflictsAreReportedNotResolvedSilently:
    """(d) Where the declaration and day_rules disagree, say so."""

    def test_the_clashing_days_are_named(self):
        _, conflicts = wa.merge_day_rules(DAY_RULES, _shape())
        text = " ".join(conflicts)
        assert "Wed" in text and "run" in text          # standing Wed run displaced
        assert "Thu" in text                            # standing Thu swim + swim focus
        assert "Fri" in text and "Sat" in text          # standing weekend rides displaced

    def test_the_declaration_is_stated_as_the_winner(self):
        _, conflicts = wa.merge_day_rules(DAY_RULES, _shape())
        assert conflicts, "a clash this size reported nothing"
        assert any("declaration wins" in c.lower() for c in conflicts)

    def test_a_declaration_that_agrees_reports_nothing(self):
        agree = {"swim_days": ["Tue", "Thu"], "declared_days": ["Tue", "Thu"]}
        _, conflicts = wa.merge_day_rules(DAY_RULES, agree)
        assert conflicts == []

    def test_the_brief_carries_them_only_when_they_exist(self):
        # planning_brief injects `declaration_conflicts` into the Stage-1 prompt verbatim,
        # so an empty key would be a config-shaped line one paraphrase from the athlete.
        assert wa.merge_day_rules(DAY_RULES, {"swim_days": ["Tue", "Thu"],
                                              "declared_days": ["Tue", "Thu"]})[1] == []


class TestTheValidatorAgrees:
    """The check none of (a)-(d) would catch. `validate_week` reads day_rules, so a
    declared MOVE (Thursday long ride against bike_days [Fri,Sat,Sun]) is a HARD
    `ride_forbidden_day` unless the caller passes the reconciled rules - a week built
    exactly as the athlete asked, failing every attempt."""

    WEEK = [
        {"start_date_local": "2026-08-11T00:00:00", "type": "Swim", "category": "WORKOUT",
         "name": "Technique + speed", "moving_time": 3600, "load_target": 45},
        {"start_date_local": "2026-08-11T18:00:00", "type": "Run", "category": "WORKOUT",
         "name": "Long run", "moving_time": 6000, "load_target": 95},
        {"start_date_local": "2026-08-12T00:00:00", "type": "Swim", "category": "WORKOUT",
         "name": "CSS", "moving_time": 3600, "load_target": 50},
        {"start_date_local": "2026-08-13T00:00:00", "type": "Ride", "category": "WORKOUT",
         "name": "Long Z2 ride", "moving_time": 14400, "load_target": 210},
        {"start_date_local": "2026-08-14T00:00:00", "type": "Run", "category": "WORKOUT",
         "name": "Easy run", "moving_time": 3600, "load_target": 55},
        {"start_date_local": "2026-08-15T00:00:00", "type": "Run", "category": "WORKOUT",
         "name": "Steady run", "moving_time": 4800, "load_target": 70},
    ]

    def _codes(self, day_rules):
        rep = validate_week(self.WEEK, MON, day_rules=day_rules)
        return {v.code for v in rep.violations if v.severity == "hard"}

    def test_raw_day_rules_hard_fail_the_declared_week(self):
        codes = self._codes(DAY_RULES)
        assert "ride_forbidden_day" in codes      # Thursday ride
        assert "run_forbidden_day" in codes       # Friday run

    def test_the_reconciled_rules_pass_it(self, tmp_path):
        wa.record("jamie", MON, base=tmp_path, source="test", **_shape())
        dr, _ = wa.effective_day_rules("jamie", MON, DAY_RULES, base=tmp_path)
        codes = self._codes(dr)
        assert "ride_forbidden_day" not in codes
        assert "run_forbidden_day" not in codes
        assert "swim_forbidden_day" not in codes


class TestOlderRecordsAndTheNegativeForm:

    def test_a_legacy_flat_file_behaves_exactly_as_before(self):
        # No declared_days anywhere: the union is {Wed}, so Wed comes out of every sport
        # and nothing else moves. That is byte-for-byte the pre-existing Phase 5a
        # behaviour, which this precedence must not change.
        dr, _ = wa.merge_day_rules(DAY_RULES, {"unavailable_days": ["Wed"]})
        assert dr["run_days"] == ["Tue", "Sat", "Sun"]
        assert dr["swim_days"] == ["Tue", "Thu"]
        assert dr["bike_days"] == ["Fri", "Sat", "Sun"]

    def test_a_whole_week_sport_exclusion_still_empties_the_sport(self):
        # Kathryn, 12 Jul 2026: "no cycling this week". Per-day precedence must not
        # reinstate it from day_rules.
        dr, conflicts = wa.merge_day_rules(
            DAY_RULES, {"bike_days": [], "excluded_sports": ["bike_days"]})
        assert dr["bike_days"] == []
        assert any("whole week" in c for c in conflicts)

    def test_a_pre_marker_record_keeps_the_meaning_it_was_written_with(self):
        # Records already on disk have no `excluded_sports`; for them an empty list DID
        # mean the sport was replaced wholesale.
        dr, _ = wa.merge_day_rules(DAY_RULES, {"bike_days": []})
        assert dr["bike_days"] == []

    def test_an_empty_sport_list_is_not_an_exclusion_once_days_are_named(self):
        # parse_day_shape_message emits all four keys every time, so a week naming no bike
        # day arrives with bike_days=[]. Reading that as "no cycling this week" would
        # cancel three rides the athlete never mentioned.
        dr, _ = wa.merge_day_rules(DAY_RULES, {"swim_days": ["Wed"], "bike_days": [],
                                               "run_days": [], "unavailable_days": [],
                                               "declared_days": ["Wed"]})
        assert dr["bike_days"] == ["Fri", "Sat", "Sun"]

    def test_a_run_limited_floor_cannot_re_add_a_declared_day(self):
        # The rehab floor keeps swim_focus days - but not on a day the athlete declared as
        # a rest or another sport. It is reported instead.
        rules = dict(DAY_RULES, swim_focus={"Tue": ["technique"], "Sun": ["css"]})
        dr, conflicts = wa.merge_day_rules(rules, _shape(), run_limited=True)
        assert "Sun" not in dr["swim_days"]                    # he is travelling
        assert any("Sun" in c for c in conflicts)

    def test_a_displaced_swim_focus_day_is_reported_once(self):
        # Jamie IS run-limited (run_protocol.quality_allowed false), so both the rehab
        # floor and the swim_focus prune see his declared Thursday ride. One line, not two.
        _, conflicts = wa.merge_day_rules(DAY_RULES, _shape(), run_limited=True)
        assert len([c for c in conflicts if "swim focus" in c]) == 1

    def test_declaring_more_runs_than_the_rehab_pattern_is_reported(self):
        # The floor trims run frequency back to the standing count. When the DECLARED days
        # alone exceed it the declaration stands, and the extra frequency must be said out
        # loud rather than resolved in silence - the run-limited athlete is Kathryn.
        rules = {"run_days": ["Tue", "Thu", "Sat"], "swim_days": [], "bike_days": []}
        decl = {"run_days": ["Mon", "Tue", "Thu", "Sat"],
                "declared_days": ["Mon", "Tue", "Thu", "Sat"]}
        dr, conflicts = wa.merge_day_rules(rules, decl, run_limited=True)
        assert dr["run_days"] == ["Mon", "Tue", "Thu", "Sat"]
        assert any(c.startswith("run: you declared 4 run days") for c in conflicts)

    def test_the_rehab_run_budget_still_trims_undeclared_days(self):
        rules = {"run_days": ["Tue", "Thu", "Sat"], "swim_days": [], "bike_days": []}
        decl = {"run_days": ["Mon"], "declared_days": ["Mon"]}
        dr, conflicts = wa.merge_day_rules(rules, decl, run_limited=True)
        assert dr["run_days"] == ["Mon", "Tue", "Thu"]      # Sat trimmed to the budget of 3
        assert not any(c.startswith("run: you declared") for c in conflicts)

    def test_a_run_limited_floor_still_keeps_an_unnamed_swim_focus_day(self):
        rules = dict(DAY_RULES, swim_focus={"Thu": ["css"]})
        partial = {"swim_days": ["Wed"], "run_days": ["Mon"],
                   "declared_days": ["Mon", "Wed"]}
        dr, _ = wa.merge_day_rules(rules, partial, run_limited=True)
        assert "Thu" in dr["swim_days"]
