"""A date is not an hours figure, and a baseline week is not asked about.

Live failure, 4 Oct 2026 08:35. Fred was asked on his Sunday card how many hours he had
for w/c 5 Oct - his baseline week, already built - and replied:

    "By next week do you mean the week starting the 12 october"

The bot logged "12 hours" for w/c 5 Oct off the date. Two faults: the ask went out for a
week the baseline block owns, and the parser read a calendar date as a weekly budget.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))

import weekly_availability as wa  # noqa: E402

FRED = "By next week do you mean the week starting the 12 october"


class TestTheLiveMessage:
    def test_yields_no_hours(self):
        assert wa.parse_hours_message(FRED)["hours"] is None

    def test_does_not_trip_either_tier(self):
        assert wa.looks_like_hours_declaration(FRED) is False
        assert wa.looks_like_hours_reply(FRED) is False

    def test_the_date_alone_is_refused_even_without_the_question(self):
        assert wa.parse_hours_message("next week, the week starting the 12 october")["hours"] is None


class TestDatesAreNotHours:
    @pytest.mark.parametrize("text", [
        "next week I'm away from 12 october",
        "next week I'm away from october 12",
        "next week I'm away from the 12th",
        "next week starting the 12",
        "next week w/c 12",
        "next week is the 12th of Oct",
    ])
    def test_a_date_is_never_the_figure(self, text):
        assert wa.parse_hours_message(text)["hours"] is None


class TestRealDeclarationsWithDatesStillWork:
    @pytest.mark.parametrize("text,hours,kept", [
        ("12 hours next week, away from 12th October", 12, "12th October"),
        ("10 hours next week, away 9 oct to 11 oct", 10, "9 oct to 11 oct"),
        ("about 12 next week", 12, ""),
        ("12 may be all I get next week", 12, ""),
    ])
    def test_the_hours_are_read_and_the_date_is_kept_as_a_constraint(self, text, hours, kept):
        p = wa.parse_hours_message(text)
        assert p["hours"] == hours
        assert kept in p["constraints"]


class TestBaselineWeekIsNotAsked:
    MON = "2026-10-05"

    def _athlete(self, base, baseline=None):
        d = base / "athletes" / "fred"
        d.mkdir(parents=True)
        (d / "profile.json").write_text(json.dumps({"max_hours_per_week": 5}))
        if baseline is not None:
            (d / "baseline.json").write_text(json.dumps(baseline))

    def _bl(self, status="active"):
        return {"status": status, "window": {"start": "2026-10-02", "end": "2026-10-11"}}

    def test_asked_with_no_baseline(self, tmp_path):
        self._athlete(tmp_path)
        assert "how many hours" in wa.sunday_hours_ask("fred", self.MON, base=tmp_path).lower()

    def test_silent_for_the_baseline_week(self, tmp_path):
        self._athlete(tmp_path, self._bl())
        assert wa.sunday_hours_ask("fred", self.MON, base=tmp_path) == ""

    def test_asked_for_the_week_after_the_baseline(self, tmp_path):
        self._athlete(tmp_path, self._bl())
        assert wa.sunday_hours_ask("fred", "2026-10-12", base=tmp_path) != ""

    def test_asked_once_the_baseline_is_complete(self, tmp_path):
        self._athlete(tmp_path, self._bl(status="complete"))
        assert wa.sunday_hours_ask("fred", self.MON, base=tmp_path) != ""
