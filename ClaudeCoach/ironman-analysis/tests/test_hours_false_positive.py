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
