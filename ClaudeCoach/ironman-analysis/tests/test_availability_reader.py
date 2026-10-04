"""Availability is read by the model (lib/availability_reader.py); code validates and merges.

4 Oct 2026: the regex capture saved Fred's "By next week do you mean the week starting the
12 october" as 12 hours, and James's "swim on Monday cycle on Wednesday" as "bike Mon; rest
Wed". The model now reads the message with the calendar and the conversation. These tests
pin what CODE still owns: which weeks can be written, the shape of what is stored, the merge
with an existing declaration, and that a failed call saves nothing.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))

import availability_reader as ar  # noqa: E402
import weekly_availability as wa  # noqa: E402

TODAY = date(2026, 10, 4)          # a Sunday
NEXT = date(2026, 10, 5)


def _reply(**kw):
    w = {"week_start": NEXT.isoformat(), "hours": None, "constraints": "",
         "swim_days": None, "bike_days": None, "run_days": None,
         "unavailable_days": None, "excluded_sports": []}
    w.update(kw.pop("week", {}))
    return json.dumps({"kind": kw.pop("kind", "declaration"), "weeks": [w],
                       "question": kw.pop("question", ""), "reason": "r"})


class TestPrompt:
    def test_the_calendar_names_real_dates_and_the_baseline_week(self):
        p = ar.build_prompt("x", today=TODAY, baseline_weeks={NEXT})
        assert "week_start 2026-10-05: Mon 5 Oct" in p and "Sun 11 Oct" in p
        assert "BASELINE WEEK" in p

    def test_a_date_is_spelled_out_as_never_hours(self):
        assert "NEVER hours" in ar.build_prompt("x", today=TODAY)

    def test_the_outstanding_ask_is_named(self):
        assert "week starting 2026-10-05" in ar.build_prompt("12", today=TODAY, asked_week=NEXT)

    def test_the_conversation_is_included(self):
        p = ar.build_prompt("Rest Tuesday not Wednesday", today=TODAY,
                            recent=[{"user": "swim Mon, bike Wed", "assistant": "Recorded"}])
        assert "ATHLETE: swim Mon, bike Wed" in p and "COACH: Recorded" in p


class TestParse:
    def test_a_declaration_round_trips(self):
        r = ar.parse(_reply(week={"hours": 12, "swim_days": ["monday", "Wed"]}), TODAY)
        assert r["kind"] == "declaration"
        w = r["weeks"][0]
        assert w["week_start"] == NEXT and w["hours"] == 12.0
        assert w["swim_days"] == ["Mon", "Wed"] and w["bike_days"] is None

    def test_a_week_outside_the_next_four_is_never_written(self):
        r = ar.parse(_reply(week={"week_start": "2026-11-30", "hours": 10}), TODAY)
        assert r["kind"] == "unclear" and r["weeks"] == [] and r["question"]

    def test_a_non_monday_is_refused(self):
        r = ar.parse(_reply(week={"week_start": "2026-10-07", "hours": 10}), TODAY)
        assert r["kind"] == "unclear"

    def test_junk_is_none(self):
        assert ar.parse("sorry, I can't help", TODAY) is None
        assert ar.parse('{"kind": "banana"}', TODAY)["kind"] == "none"

    def test_unclear_always_carries_a_question(self):
        assert ar.parse(_reply(kind="unclear"), TODAY)["question"]


class TestRead:
    def test_a_failed_call_saves_nothing(self):
        def boom(_):
            raise RuntimeError("capped")
        assert ar.read("12 hours next week", today=TODAY, call=boom) is None

    def test_an_empty_declaration_is_none(self):
        r = ar.read("x", today=TODAY, call=lambda _: _reply())
        assert r["kind"] == "none"

    def test_freds_question_is_not_saved_when_the_model_says_none(self):
        r = ar.read("By next week do you mean the week starting the 12 october",
                    today=TODAY, call=lambda _: json.dumps({"kind": "none", "weeks": []}))
        assert r["kind"] == "none"


class TestMerge:
    def test_unmentioned_fields_are_carried_forward(self):
        prior = {"hours": 10, "constraints": "away Thu", "run_days": ["Sat"],
                 "declared_days": ["Sat"], "rest_day_waiver": "ok Sun"}
        w = {"hours": None, "constraints": "", "swim_days": ["Mon"], "bike_days": None,
             "run_days": None, "unavailable_days": ["Tue"], "excluded_sports": []}
        rec = ar.merged(prior, w)
        assert rec["hours"] == 10 and rec["constraints"] == "away Thu"
        assert rec["run_days"] == ["Sat"] and rec["swim_days"] == ["Mon"]
        assert rec["declared_days"] == ["Mon", "Tue", "Sat"]
        assert rec["rest_day_waiver"] == "ok Sun"

    def test_an_earlier_exclusion_survives_unless_the_sport_gets_a_day(self):
        prior = {"bike_days": [], "excluded_sports": ["bike"]}
        keep = ar.merged(prior, {"hours": 8, "excluded_sports": []})
        assert keep["excluded_sports"] == ["bike"] and keep["bike_days"] == []
        back = ar.merged(prior, {"bike_days": ["Wed"], "excluded_sports": []})
        assert "excluded_sports" not in back and back["bike_days"] == ["Wed"]

    def test_the_merged_record_is_storable(self, tmp_path):
        (tmp_path / "athletes" / "x").mkdir(parents=True)
        rec = ar.merged(None, {"hours": 9, "constraints": "", "swim_days": ["Mon"],
                               "unavailable_days": ["Tue"], "excluded_sports": ["run"]})
        wa.record("x", NEXT, source="test", base=tmp_path, **rec)
        got = wa.for_week("x", NEXT, base=tmp_path)
        assert got["hours"] == 9 and got["swim_days"] == ["Mon"] and got["run_days"] == []

    def test_describe(self):
        assert ar.describe({"hours": 9, "swim_days": ["Mon"], "unavailable_days": ["Tue"],
                            "excluded_sports": ["run"]}) == "9 hours; swim Mon; nothing Tue; no run"


class TestClearWeek:
    def test_clears_only_that_week(self, tmp_path):
        (tmp_path / "athletes" / "x").mkdir(parents=True)
        wa.record("x", NEXT, hours=12, base=tmp_path)
        wa.record("x", date(2026, 10, 12), hours=8, base=tmp_path)
        assert wa.clear_week("x", NEXT, base=tmp_path)["hours"] == 12
        assert wa.for_week("x", NEXT, base=tmp_path) is None
        assert wa.hours_for_week("x", date(2026, 10, 12), base=tmp_path) == 8
        assert wa.clear_week("x", NEXT, base=tmp_path) is None


class TestWorthReading:
    def test_while_the_ask_is_open_everything_is_read(self):
        assert ar.worth_reading("12", ask_outstanding=True)

    def test_availability_words_are_read(self):
        for t in ("away Thu to Sun", "I have 10h", "nothing on Tuesday", "no cycling for me",
                  "next week is busy"):
            assert ar.worth_reading(t), t

    def test_chat_with_nothing_about_time_is_not(self):
        for t in ("thanks!", "felt great, RPE 6", "how was my ride"):
            assert not ar.worth_reading(t), t
