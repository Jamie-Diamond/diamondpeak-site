"""A multi-topic message must not let one clause's "week" attach to another
clause's number, and must not let a question's own number get written at all.

Live failure 1, Kathryn, 23 Aug 2026 22:06. She sent:

    "Tuesday to Thursday, total exercise cannot be longer than 2.5 hours. This week I
     can move swimming to either Tuesday, Wednesday or Thursday. Based on this, please
     replan"

and the auto-capture stored 2.5 as the WEEK's hours budget — the validator then
asserted a 130-Load cap on a 685-Load week. The "This week" framing lives in the
second sentence; the 2.5h figure is a per-day-range cap in the first. Same misparse
happened again on 3 Aug (per the bot's own admission in the transcript).

Live failure 2, Jamie, 23 Aug 2026 22:30 (same day, different athlete). Kathryn's
message:

    "...How would you replan the week with that information?

     Why is my Saturday ride over 4 hours?

     Friday does not have to be the rest day..."

wrote 4 as the week's hours budget. "Why is my Saturday ride over 4 hours?" is a
question about a single ride, not a declaration — but the message-level question
check only looks at the START of the whole string (or a few fixed idioms), so a
question buried in the middle of a longer message was invisible to it.

Both are the same root cause: parse_hours_message read signals from the WHOLE
message as one blob instead of checking that the figure's own clause was the one
doing the framing (or wasn't disqualified in its own right).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))

import weekly_availability as wa  # noqa: E402

KATHRYN_DAY_SCOPED_CAP = (
    "Tuesday to Thursday, total exercise cannot be longer than 2.5 hours. "
    "This week I can move swimming to either Tuesday, Wednesday or Thursday. "
    "Based on this, please replan")

KATHRYN_EMBEDDED_QUESTION = (
    "It is meant to rain Wednesday, Thursday, Friday, Saturday, Sunday. "
    "I can do kettlebells on Thursday instead of Monday. "
    "How would you replan the week with that information?\n\n"
    "Why is my Saturday ride over 4 hours? \n\n"
    "Friday does not have to be the rest day, I am just removing the constraint of "
    "only swimming on that day")


class TestDayScopedCapDoesNotBorrowAnotherClausesWeek:
    def test_the_exact_message_yields_no_hours(self):
        assert wa.parse_hours_message(KATHRYN_DAY_SCOPED_CAP)["hours"] is None

    def test_the_exact_message_does_not_trip_tier1(self):
        assert wa.looks_like_hours_declaration(KATHRYN_DAY_SCOPED_CAP) is False

    def test_the_exact_message_does_not_trip_tier2(self):
        assert wa.looks_like_hours_reply(KATHRYN_DAY_SCOPED_CAP) is False


class TestEmbeddedQuestionIsNeverADeclaration:
    def test_the_exact_message_yields_no_hours(self):
        assert wa.parse_hours_message(KATHRYN_EMBEDDED_QUESTION)["hours"] is None

    def test_the_exact_message_does_not_trip_tier1(self):
        assert wa.looks_like_hours_declaration(KATHRYN_EMBEDDED_QUESTION) is False

    def test_the_exact_message_does_not_trip_tier2(self):
        assert wa.looks_like_hours_reply(KATHRYN_EMBEDDED_QUESTION) is False


class TestGenuineCrossClauseDeclarationsStillWork:
    """The fix must stay narrow: a day RANGE spelled with "to" inside the figure's
    own clause is what disqualifies it — a plain hyphenated aside elsewhere in the
    message must keep registering, exactly as it did before this fix."""

    def test_hyphenated_day_aside_still_registers(self):
        p = wa.parse_hours_message("about 12 hours, away Thu-Fri")
        assert p["hours"] == 12.0

    def test_week_framed_day_range_in_the_same_clause_still_registers(self):
        p = wa.parse_hours_message("12 hours this week, nothing long Mon-Thu")
        assert p["hours"] == 12.0
