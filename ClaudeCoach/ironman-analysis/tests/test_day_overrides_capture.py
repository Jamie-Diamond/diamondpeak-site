"""Recording a directed day override from chat — the capture half of lib/day_overrides.py.

The register is FAIL-CLOSED and this is the strictest capture in the tree, because a wrongly
recorded override does not write a wrong fact, it SILENCES A CHECK: a hard
`{sport}_forbidden_day` becomes a soft advisory, so a generator genuinely drifting off an
athlete's pattern stops being reported. Every test here is about refusing.
"""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import day_overrides as do        # noqa: E402
import open_actions as oa         # noqa: E402
import races                      # noqa: E402
import weekly_availability as wa  # noqa: E402

MON = date(2026, 7, 27)     # Monday
THU = date(2026, 7, 30)     # Thursday of the same week
WED = "2026-07-29"

# Jamie's real pattern: swim Tue/Thu, so Wednesday is off-pattern for swim and NOT for run.
JAMIE = {"swim_days": ["Tue", "Thu"], "bike_days": ["Fri", "Sat", "Sun"],
         "run_days": ["Tue", "Wed", "Sat", "Sun"], "strength_max": 2}


class TestCapturesAGenuineDirection:
    @pytest.mark.parametrize("msg", [
        "swim Wednesday this week",
        "I told it this week to swim on wed",
        "move the swim to Wednesday",
        "let's swim Wed instead",
        "I'm swimming Wednesday this week",
        "swim on 2026-07-29",
    ])
    def test_parsed(self, msg):
        p = do.parse_directed_day(msg, MON)
        assert p["family"] == "swim", msg
        assert p["date"] == WED, msg

    def test_the_coach_phrasing_that_motivated_the_register(self):
        # "I told it this week to swim on wed, so we swim on wed, rules are guidelines."
        p = do.parse_directed_day("I told it this week to swim on wed", MON)
        assert (p["family"], p["date"]) == ("swim", WED)

    def test_bike_synonyms(self):
        for msg in ("move the ride to Wednesday", "let's turbo Wed instead"):
            assert do.parse_directed_day(msg, MON)["family"] == "bike", msg


class TestFailsClosed:
    @pytest.mark.parametrize("msg", [
        "should I swim Wednesday?",
        "what about swimming Wednesday",
        "can I swim Wed instead",
        "is Wednesday a swim day",
    ])
    def test_a_question_never_records(self, msg):
        p = do.parse_directed_day(msg, MON)
        assert not (p["family"] and p["date"]), msg

    @pytest.mark.parametrize("msg", [
        "I swam on Wednesday",
        "swam Wed",
        "did the swim Wednesday",
        "ended up swimming Wednesday",
    ])
    def test_a_report_of_what_happened_never_records(self, msg):
        # The dangerous case. Recording this would retro-excuse a deviation NOBODY
        # directed, which is the exact fail-open the register exists to prevent.
        p = do.parse_directed_day(msg, MON)
        assert not (p["family"] and p["date"]), msg

    @pytest.mark.parametrize("msg", [
        "swim Wed or Thu",             # a choice, not one session
        "swim and run Wednesday",      # two sports, one key
        "swim this week",              # no day
        "Wednesday this week",         # no sport
        "nothing long Mon-Thu",        # an hours constraint
        "swim sometime midweek",
    ])
    def test_anything_ambiguous_records_nothing(self, msg):
        p = do.parse_directed_day(msg, MON)
        assert not (p["family"] and p["date"]), msg
        assert p["refused"], msg

    def test_a_bare_weekday_already_past_this_week_is_refused(self):
        # Said on Thursday, "swim Wednesday" could mean the Wednesday just gone or the one
        # coming. lib/races.resolve_date would roll it FORWARD to next Wednesday — writing
        # a permission for a day a week from the one meant. Refused instead.
        assert do.resolve_directed_date("swim Wednesday", THU) is None
        assert do.parse_directed_day("swim Wednesday", THU)["refused"] == \
            "no unambiguous single date"

    def test_this_week_and_next_week_disambiguate_it(self):
        assert do.resolve_directed_date("swim Wednesday this week", THU) == date(2026, 7, 29)
        assert do.resolve_directed_date("swim Wednesday next week", THU) == date(2026, 8, 5)

    def test_a_bare_weekday_still_to_come_resolves(self):
        assert do.resolve_directed_date("swim Friday", THU) == date(2026, 7, 31)

    def test_today_resolves_to_today_not_a_week_away(self):
        assert do.resolve_directed_date("swim Thursday", THU) == THU
        assert do.resolve_directed_date("swim today", THU) == THU


class TestOffPatternGate:
    def test_an_off_pattern_day_is_capturable(self):
        assert do.is_off_pattern(JAMIE, "swim", WED) is True

    def test_an_on_pattern_day_excuses_nothing(self):
        # No hard forbidden-day failure exists for a Tuesday swim, so an override would be
        # a permission with nothing to permit — and entries are counted per sport+weekday
        # for the `day_rules_drifted` alarm, so a redundant one pushes towards a false one.
        assert do.is_off_pattern(JAMIE, "swim", "2026-07-28") is False
        assert do.is_off_pattern(JAMIE, "run", WED) is False

    def test_a_sport_with_no_rule_list_is_not_capturable(self):
        # Calum has bike_days only. validate_plan raises no swim forbidden-day for him.
        assert do.is_off_pattern({"bike_days": ["Mon", "Tue"]}, "swim", WED) is False

    def test_absent_day_rules_are_not_capturable(self):
        assert do.is_off_pattern(None, "swim", WED) is False
        assert do.is_off_pattern({}, "swim", WED) is False


class TestRegisterWriteAndFailClosedRead:
    def test_record_then_load(self, tmp_path):
        k = do.record("jamie", tmp_path, "swim", WED, do.capture_note("Telegram"))
        assert k == f"swim:{WED}"
        assert do.load("jamie", tmp_path)[k].startswith("Coach-directed in Telegram")

    def test_the_note_records_provenance(self):
        note = do.capture_note("Telegram", date(2026, 7, 29))
        assert "Telegram" in note and "2026-07-29" in note

    def test_a_note_is_mandatory(self, tmp_path):
        # An entry with no provenance is indistinguishable from a permission granted by
        # accident.
        with pytest.raises(ValueError):
            do.record("jamie", tmp_path, "swim", WED, "")

    def test_a_bad_family_is_refused(self, tmp_path):
        with pytest.raises(ValueError):
            do.record("jamie", tmp_path, "strength", WED, "note")

    def test_a_corrupt_register_grants_nothing(self, tmp_path):
        p = do.register_path("jamie", tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ this is not json")
        assert do.load("jamie", tmp_path) == {}

    def test_a_malformed_entry_is_dropped_not_honoured(self, tmp_path):
        do.record("jamie", tmp_path, "swim", WED, "good note")
        p = do.register_path("jamie", tmp_path)
        blob = json.loads(p.read_text())
        blob["swim:not-a-date"] = "nonsense key"
        blob["run:2026-07-29"] = ""              # empty value
        p.write_text(json.dumps(blob))
        reg = do.load("jamie", tmp_path)
        assert set(reg) == {f"swim:{WED}"}

    def test_granularity_is_one_sport_on_one_date(self, tmp_path):
        do.record("jamie", tmp_path, "swim", WED, "n")
        reg = do.load("jamie", tmp_path)
        # A second, UNDIRECTED swim deviation that week must not hide behind this one.
        assert "swim:2026-07-30" not in reg


class TestCrossFireWithOtherCaptures:
    @pytest.mark.parametrize("msg", ["swim Wednesday this week", "move the swim to Wednesday"])
    def test_a_day_direction_is_not_read_as_hours(self, msg):
        assert not wa.looks_like_hours_declaration(msg), msg
        assert not wa.looks_like_hours_reply(msg), msg

    @pytest.mark.parametrize("msg", ["swim Wednesday this week", "let's swim Wed instead"])
    def test_a_day_direction_is_not_read_as_a_race(self, msg):
        assert not races.looks_like_race_statement(msg, MON), msg

    def test_the_one_genuinely_overlapping_phrasing_is_resolved_by_dispatch_ORDER(self):
        # "move the swim to Wednesday" satisfies BOTH detectors: "move ... to" is a deferral
        # verb in open_actions. The detectors cannot be made disjoint without crippling one
        # of them, so the overlap is real and is resolved by which handler runs first.
        msg = "move the swim to Wednesday"
        assert do.parse_directed_day(msg, MON)["family"] == "swim"
        # open_actions has since grown its own guard for this exact phrasing (a plan day +
        # a session word that parses as a day directive is not an action instruction), so
        # it now stands DOWN rather than relying on dispatch order. Asserted as the primary
        # defence; the source order below is the backstop if that guard is ever relaxed.
        assert not oa.looks_like_action_instruction(msg)

        # ...and telegram/bot.py routes the day-rule handler FIRST, because it is the more
        # specific of the two: it demands a sport, an unambiguous date and a genuinely
        # off-pattern day, where action capture needs only a substring of a label. Assert
        # the order in the source, since only ONE capture may fire per message and a future
        # reorder would silently send this phrasing elsewhere.
        #
        # Updated 13 Aug 2026: the four captures now dispatch from a tuple in _route_text
        # rather than a chain of `if handler(...): return`. The ordering guarantee is the
        # same (the loop breaks on the first capture that fires) but a fired capture no
        # longer STOPS the message — it returns a note and the message still reaches the
        # model, so the athlete's instruction gets carried out as well as recorded.
        bot = (Path(__file__).resolve().parents[2] / "telegram/bot.py").read_text()
        day_at = bot.index("_handle_dayrule_capture,   #")
        act_at = bot.index("_handle_action_capture):   #")
        assert day_at < act_at, "day-rule capture must dispatch before action capture"

    def test_a_day_direction_with_no_deferral_verb_is_not_an_action_instruction(self):
        for msg in ("swim Wednesday this week", "let's swim Wed instead",
                    "I told it this week to swim on wed"):
            assert not oa.looks_like_action_instruction(msg), msg

    @pytest.mark.parametrize("msg", [
        "14h next week", "12 hours this week, nothing long Mon-Thu", "20, big week",
    ])
    def test_an_hours_declaration_is_not_read_as_a_day_direction(self, msg):
        p = do.parse_directed_day(msg, MON)
        assert not (p["family"] and p["date"]), msg

    @pytest.mark.parametrize("msg", [
        "sweat test is booked", "drop the ISM saddle order",
        "push the ice retention test to next week",
    ])
    def test_an_action_instruction_is_not_read_as_a_day_direction(self, msg):
        p = do.parse_directed_day(msg, MON)
        assert not (p["family"] and p["date"]), msg

    def test_a_race_statement_is_not_read_as_a_day_direction(self):
        for m in ("I'm racing Dorney on Saturday", "I'm racing the Outlaw on 26 July"):
            p = do.parse_directed_day(m, MON)
            assert not (p["family"] and p["date"]), m
