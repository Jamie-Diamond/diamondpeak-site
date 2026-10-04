"""One model read per message for the four chat captures (lib/capture_reader.py).

Race, day-change and action capture moved from regex parsers to the model on 4 Oct 2026,
the same day as availability, at Jamie's request. These pin what CODE still owns: the
validation of what the model returns, so a bad date, an out-of-range day or an item number
that does not exist can never be written or offered.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))

import capture_reader as cr  # noqa: E402

TODAY = date(2026, 10, 4)
NONE = {"availability": {"kind": "none", "weeks": []}, "race": {"found": False},
        "day_change": {"found": False}, "action": {"found": False}}


def _parse(n_actions=3, **parts):
    return cr.parse(json.dumps({**NONE, **parts}), TODAY, n_actions)


class TestRace:
    def test_a_future_race_is_kept(self):
        r = _parse(race={"found": True, "name": "Dorney sprint", "date": "2026-11-01",
                         "priority": "b"})["race"]
        assert r == {"found": True, "name": "Dorney sprint", "date": "2026-11-01",
                     "priority": "B", "reason": ""}

    def test_a_past_or_far_off_race_is_dropped(self):
        assert not _parse(race={"found": True, "name": "X", "date": "2026-09-19"})["race"]["found"]
        assert not _parse(race={"found": True, "name": "X", "date": "2030-01-01"})["race"]["found"]

    def test_no_name_or_no_date_is_dropped(self):
        assert not _parse(race={"found": True, "name": "", "date": "2026-11-01"})["race"]["found"]
        assert not _parse(race={"found": True, "name": "X", "date": "soon"})["race"]["found"]

    def test_an_unknown_priority_is_left_unset_not_guessed(self):
        r = _parse(race={"found": True, "name": "X", "date": "2026-11-01", "priority": "main"})
        assert r["race"]["priority"] is None


class TestDayChange:
    def test_a_day_in_the_next_four_weeks_is_kept(self):
        d = _parse(day_change={"found": True, "sport": "swim", "date": "2026-10-07"})
        assert d["day_change"]["found"] and d["day_change"]["sport"] == "swim"

    def test_a_past_day_or_an_unknown_sport_is_dropped(self):
        assert not _parse(day_change={"found": True, "sport": "swim",
                                      "date": "2026-09-30"})["day_change"]["found"]
        assert not _parse(day_change={"found": True, "sport": "gym",
                                      "date": "2026-10-07"})["day_change"]["found"]

    def test_beyond_the_calendar_is_dropped(self):
        assert not _parse(day_change={"found": True, "sport": "run",
                                      "date": "2026-11-30"})["day_change"]["found"]


class TestAction:
    def test_a_listed_item_is_kept(self):
        a = _parse(action={"found": True, "items": [1], "status": "done"})["action"]
        assert a["found"] and a["items"] == [1] and a["status"] == "done"

    def test_an_item_number_that_does_not_exist_is_dropped(self):
        assert not _parse(action={"found": True, "items": [7], "status": "done"})["action"]["found"]
        assert not _parse(n_actions=0, action={"found": True, "items": [0],
                                               "status": "done"})["action"]["found"]

    def test_an_unknown_status_is_dropped(self):
        assert not _parse(action={"found": True, "items": [0],
                                  "status": "nearly"})["action"]["found"]

    def test_a_defer_keeps_only_a_future_date(self):
        a = _parse(action={"found": True, "items": [0], "status": "defer",
                           "defer_to": "2026-10-20"})["action"]
        assert a["defer_to"] == "2026-10-20"
        a = _parse(action={"found": True, "items": [0], "status": "defer",
                           "defer_to": "2026-09-01"})["action"]
        assert a["found"] and a["defer_to"] is None          # the bot then asks for a date


class TestReadAndPrompt:
    def test_a_failed_call_records_nothing(self):
        def boom(_):
            raise RuntimeError("capped")
        assert cr.read("racing Dorney on 1 Nov", today=TODAY, call=boom) is None

    def test_junk_records_nothing(self):
        assert cr.read("x", today=TODAY, call=lambda _: "no idea") is None

    def test_the_prompt_lists_the_actions_and_usual_days(self):
        p = cr.build_prompt("x", today=TODAY, actions=["Sweat test", "Bike fit"],
                            day_rules={"swim_days": ["Tue", "Thu"]})
        assert "0. Sweat test" in p and "1. Bike fit" in p and "swim Tue/Thu" in p


class TestWorthReading:
    def test_race_and_move_language_is_read(self):
        for t in ("racing Dorney", "entered the marathon", "I'll swim instead"):
            assert cr.worth_reading(t), t

    def test_to_do_language_only_when_there_are_open_items(self):
        assert cr.worth_reading("sweat test booked", has_actions=True)
        assert not cr.worth_reading("sweat test booked", has_actions=False)

    def test_small_talk_is_not_read(self):
        assert not cr.worth_reading("thanks!", has_actions=True)
