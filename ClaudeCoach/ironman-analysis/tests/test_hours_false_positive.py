"""The hours capture must not read a distance, pace, power or HR as a weekly budget.

Live failure, 3 Aug 2026 14:15. Jamie sent:

    "So actually the wheels really came off at 15-20k not 30k they were already in a bad
     place. Add a cumulative pace column"

and the bot replied:

    "Just so I've got this right - is that 15 hours of training for next week (So actually
     the wheels really came off at k not 30k they were already in a bad place. Add a
     cumulative pace column)?"   [Yes, that's my week] [No]

Two faults compounding. _lower_of_range matched "15-20" from "15-20k": both ends sit
inside the plausible weekly range and ascend, and nothing checked for a following UNIT.
Then _strip_hours_phrase removed "15-20" from his sentence and pasted the remainder in as
"constraints", producing "came off at k not 30k". The result reads unhinged, and a Yes
would have written 15 hours as the week's ceiling.

Root cause is the same shape as everything else in this system: a number was taken from
free text without checking what it was a number OF.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))

import weekly_availability as wa  # noqa: E402

REAL_MESSAGE = ("So actually the wheels really came off at 15-20k not 30k they were "
                "already in a bad place. Add a cumulative pace column")


class TestTheLiveFalsePositive:
    def test_the_exact_message_yields_no_hours(self):
        assert wa.parse_hours_message(REAL_MESSAGE)["hours"] is None

    def test_the_exact_message_does_not_trip_tier2(self):
        """TIER 2 is the path that actually fired, because a Sunday ask was outstanding."""
        assert wa.looks_like_hours_reply(REAL_MESSAGE) is False

    def test_the_exact_message_does_not_trip_tier1(self):
        assert wa.looks_like_hours_declaration(REAL_MESSAGE) is False


class TestUnitsAreNotHours:
    @pytest.mark.parametrize("text", [
        "the wheels came off at 15-20k",          # distance range
        "ran 5:13/km for the first 30k",          # pace
        "held 250-280w on the climbs",            # power
        "HR was 145-155 bpm",                     # heart rate
        "did 20k this morning",                   # distance
        "12kg lighter than last year",            # mass
        "it was 25-30°C out there",               # temperature
        "swim was 45min",                         # minutes, not hours
        "8-10 min reps",                          # interval minutes
    ])
    def test_a_number_with_a_unit_is_never_hours(self, text):
        assert wa.parse_hours_message(text)["hours"] is None, text


class TestRealDeclarationsStillWork:
    """The fix must not deafen the capture: these are the shapes that SHOULD register."""

    @pytest.mark.parametrize("text,hours", [
        ("12 hours next week", 12.0),
        ("I have 12-13 hours next week", 12.0),
        ("about 12 hours, away Thu-Fri", 12.0),
        ("14", 14.0),
        ("maybe 9.5 hours this week", 9.5),
    ])
    def test_declared_hours_are_captured(self, text, hours):
        assert wa.parse_hours_message(text)["hours"] == hours


class TestReplyLengthGuard:
    """An answer to "how many hours next week?" is short. A paragraph is not an answer,
    however many numbers it happens to contain."""

    def test_long_message_cannot_be_an_hours_reply(self):
        long_msg = " ".join(["word"] * 20) + " 12"
        assert wa.looks_like_hours_reply(long_msg) is False

    def test_short_message_still_can(self):
        assert wa.looks_like_hours_reply("12") is True
        assert wa.looks_like_hours_reply("about 12 hours") is True


class TestAskWindowClosesAfterOneMessage:
    """The mechanism fix, not another regex.

    The ask used to stay live for 36 hours, and during that whole window ANY one-or-two
    digit number in ANY message was a candidate answer. That is a text scanner pretending
    to be a form field. It is why the same bug fired four times in 24 hours on 3 Aug 2026
    with four different trigger shapes: a distance range, an elapsed-minutes reference and
    two single-day figures.

    Now the ask is answerable by the NEXT thing the athlete says and nothing after it.
    """

    @pytest.fixture
    def base(self, tmp_path):
        (tmp_path / "athletes" / "j").mkdir(parents=True)
        return tmp_path

    def test_ask_is_live_until_the_first_message(self, base):
        from datetime import date
        ws = date(2026, 8, 10)
        wa.note_ask_sent("j", ws, base=base)
        assert wa.ask_outstanding("j", ws, base=base) is True

    def test_ask_closes_once_the_athlete_says_something_else(self, base):
        from datetime import date
        ws = date(2026, 8, 10)
        wa.note_ask_sent("j", ws, base=base)
        wa.consume_ask("j", ws, base=base)
        assert wa.ask_outstanding("j", ws, base=base) is False

    def test_consume_is_idempotent(self, base):
        from datetime import date
        ws = date(2026, 8, 10)
        wa.note_ask_sent("j", ws, base=base)
        wa.consume_ask("j", ws, base=base)
        wa.consume_ask("j", ws, base=base)
        assert wa.ask_outstanding("j", ws, base=base) is False

    def test_legacy_string_shape_is_still_readable_and_consumable(self, base):
        """Existing files store the ask as a bare ISO string. That must keep working, and
        must be closable, or a live athlete file would be permanently exposed."""
        import json
        from datetime import date, datetime
        ws = date(2026, 8, 10)
        p = base / "athletes" / "j" / "this-week-availability.json"
        p.write_text(json.dumps({
            "asks": {ws.isoformat(): datetime.now().isoformat(timespec="seconds")},
            "declarations": []}))
        assert wa.ask_outstanding("j", ws, base=base) is True
        wa.consume_ask("j", ws, base=base)
        assert wa.ask_outstanding("j", ws, base=base) is False

    def test_a_real_declaration_still_lands_with_no_ask_at_all(self):
        """Closing the window must not cost the athlete the ability to declare hours
        whenever they like - that path never needed an outstanding ask."""
        assert wa.looks_like_hours_declaration("14 hours next week") is True
        assert wa.parse_hours_message("14 hours next week")["hours"] == 14.0
